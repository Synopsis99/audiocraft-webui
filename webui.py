from flask import Flask, render_template, request,jsonify
from wtforms import Form, TextAreaField, FileField, SelectField, IntegerField, FloatField, SubmitField
from wtforms.validators import DataRequired, NumberRange
from scipy.io import wavfile
import numpy as np
import os, re, json, sys
import torch, torchaudio
from audiocraft.models import MusicGen
from operator import itemgetter
import librosa
import soundfile as sf

MODEL = None
unload = False

if len(sys.argv) > 1:
    if sys.argv[1] == "--unload-after-gen":
        unload = True

def load_model(version):
    print("Loading model", version)
    model = None
    try:
        model = MusicGen.get_pretrained(version)
    except Exception as e:
        print(f"Failed to load model due to error: {e}, you probably need to pick a smaller model.")
        torch.cuda.empty_cache()
        torch.cuda.synchronize()
        return None
    return model

def load_and_process_audio(melody_data, sr, model):
    if melody_data is not None:
        melody = torch.from_numpy(melody_data).to(model.device).float().t().unsqueeze(0)
        if melody.dim() == 2:
            melody = melody[None]
        melody = melody[..., :int(sr * model.lm.cfg.dataset.segment_duration)]
        return melody
    else:
        return None
    

def sanitize_filename(filename):
    """
    Takes a filename and returns a sanitized version safe for filesystem operations.
    """
    return re.sub(r'[^\w\d-]', ' ', filename)

def save_output(output, text):
    """
    Save output to a WAV file with the filename based on the input text.
    If a file with the same name already exists, append a number in parentheses.
    """
    i = 1
    base_filename = f"static/audio/{sanitize_filename(text)}.wav"
    output_filename = base_filename
    while os.path.exists(output_filename):
        output_filename = f"{base_filename.rsplit('.', 1)[0]}({i}).wav"
        i += 1

    wavfile.write(output_filename, output[0], np.array(output[1], dtype=np.float32))
    return output_filename

#From https://colab.research.google.com/drive/154CqogsdP-D_TfSF9S2z8-BY98GN_na4?usp=sharing#scrollTo=exKxNU_Z4i5I
#Thank you DragonForged
def extend_audio(model, prompt_waveform, prompt, prompt_sr, num_segments=5, overlap=2):
    # Calculate the number of samples corresponding to the overlap
    overlap_samples = int(overlap * prompt_sr)

    device = model.device
    prompt_waveform = prompt_waveform.to(device)
    
    print(num_segments)
    print(overlap)

    for _ in range(num_segments):
        # Grab the end of the waveform
        end_waveform = prompt_waveform[...,-overlap_samples:]

        # Process the trimmed waveform using the model
        new_audio = model.generate_continuation(end_waveform, descriptions=[prompt], prompt_sample_rate=prompt_sr, progress=True)
            
        # Cut the seed audio off the newly generated audio
        new_audio = new_audio[...,overlap_samples:]

        prompt_waveform = torch.cat([prompt_waveform, new_audio], dim=2)

    return prompt_waveform.detach().cpu().numpy()


def predict(model, text, melody, sr, duration, topk, topp, temperature, cfg_coef, segments, overlap):
    global MODEL
    topk = int(topk)
    if MODEL is None or MODEL.name != model:
        if MODEL is not None:
            del MODEL
            torch.cuda.empty_cache()
            torch.cuda.synchronize()
        MODEL = load_model(model)
        if MODEL is None:
            return None
        return predict(model, text, melody, sr, duration, topk, topp, temperature, cfg_coef, segments, overlap)

    #if duration > MODEL.lm.cfg.dataset.segment_duration:
    #    raise gr.Error("MusicGen currently supports durations of up to 30 seconds!")
    MODEL.set_generation_params(
        use_sampling=True,
        top_k=topk,
        top_p=topp,
        temperature=temperature,
        cfg_coef=cfg_coef,
        duration=duration,
    )
    
    melody = load_and_process_audio(melody, sr, MODEL)

    if melody is not None:
        output = MODEL.generate_with_chroma(
            descriptions=[text],
            melody_wavs=melody,
            melody_sample_rate=sr,
            progress=False
        )
    else:
        output = MODEL.generate(descriptions=[text], progress=False)

    sample_rate = MODEL.sample_rate
    
    if segments > 1:
        output_tensors = extend_audio(MODEL, output, text, sample_rate, segments, overlap)
    else:
        output_tensors = output.detach().cpu().numpy()
    
    if unload:
        del output
        import gc
        del MODEL
        MODEL = None
        gc.collect() 
        torch.cuda.empty_cache()
        torch.cuda.synchronize()
    
    return sample_rate, output_tensors

class MusicForm(Form):
    text = TextAreaField('Input Text', [DataRequired()])
    melody = FileField('Optional Melody')
    model = SelectField('Model', choices=[('melody', 'melody'), ('medium', 'medium'), ('small', 'small'), ('large', 'large')], default='large')
    duration = IntegerField('Duration', default=10, validators=[NumberRange(min=1, max=30)])
    topk = IntegerField('Top-k', default=250)
    topp = FloatField('Top-p', default=0)
    temperature = FloatField('Temperature', default=1.0)
    cfg_coef = FloatField('Classifier Free Guidance', default=7.0)
    segments = IntegerField('Segments', default=5, validators=[NumberRange(min=1, max=10)])
    overlap = FloatField('Overlap', default=5.0) 
    submit = SubmitField('Submit')

app = Flask(__name__)

@app.route('/api/audio-files', methods=['GET'])
def get_audio_files():
    audio_files = [(f, f, os.path.getmtime(f'static/audio/{f}')) for f in os.listdir('static/audio')]
    audio_files_dicts = [{'text': text, 'audio_file': audio_file, 'timestamp': timestamp} for text, audio_file, timestamp in audio_files]
    return jsonify(audio_files_dicts)

@app.route('/', methods=['GET', 'POST'])
def home_and_submit():
    form = MusicForm(request.form)
    output_filename = None  # Initialize output_filename here

    if request.method == 'POST' and form.validate():
        model = form.model.data
        text = form.text.data
        duration = form.duration.data
        topk = form.topk.data
        topp = form.topp.data
        temperature = form.temperature.data
        cfg_coef = form.cfg_coef.data
        segments = form.segments.data
        overlap = form.overlap.data
        
        parameters = {
            "model": model,
            "text": text,
            "duration": duration,
            "topk": topk,
            "topp": topp,
            "temperature": temperature,
            "cfg_coef": cfg_coef,
            "segments": segments,
            "overlap": overlap
        }

        for name, value in parameters.items():
            print(f"{name}: {value}")

        melody = None
        sr = None
        if 'melody' in request.files and request.files['melody'].filename != '':
            if form.model.data != 'melody':
                pass
            else:
                melody_file = request.files['melody']
                extension = os.path.splitext(melody_file.filename)[1]
                if extension.lower() in ['.wav', '.mp3']:
                    melody, sr = librosa.load(melody_file, sr=None)
                else:
                    print(f"Unsupported file extension: {extension}")

        output = predict(model, text, melody, sr, duration, topk, topp, temperature, cfg_coef, segments, overlap)
        if output is None:
            return None
        output_filename = save_output(output, form.text.data)
        
    if not os.path.exists('static/audio'):
        os.makedirs('static/audio')

    audio_files = [(f, f, os.path.getmtime(f'static/audio/{f}')) for f in os.listdir('static/audio')]
    audio_files_dicts = [{'text': text, 'audio_file': audio_file, 'timestamp': timestamp} for text, audio_file, timestamp in audio_files]
    
    if request.method == 'POST':
        # If the output_filename is not None, find the corresponding file in the audio_files list and return it
        if output_filename is not None:
            output_filename = os.path.basename(output_filename)
            new_file = next((file for file in audio_files_dicts if file['audio_file'] == output_filename), None)
            return jsonify(new_file)
        else:
            return jsonify({'error': 'No new file generated.'})
    else:
        return render_template('form.html', form=form, audio_files=audio_files_dicts)

if __name__ == '__main__':
    if not os.path.exists('static/audio'):
        os.makedirs('static/audio')
    app.run(debug=True)