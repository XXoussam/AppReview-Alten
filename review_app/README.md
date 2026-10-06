# Clip review app

One private web interface for the consultant team to turn a game video into a validated
speech dataset:

1. **Upload** a game video (MP4, MKV, MOV, WebM, AVI, TS, FLV, or an audio file).
2. The app **extracts the audio** and **cuts it into clips**. Every clip keeps its `start` and
   `end` as seconds in the original video.
3. **Transcribe** the clips with the ASR model (Parakeet now, the fine-tuned checkpoint later),
   either inside the app or from a separate GPU job that pushes its results through the API.
4. A reviewer listens to each segment, **played directly in the original video** at the right
   timestamp, marks the transcript **Correcte** or **Corrige** it, picks the **player** who is
   talking, scores **PAD** (Plaisir, Activation, Dominance, 1 to 7) and adds notes.
5. **Export** the validated clips as a NeMo manifest (JSONL) for fine-tuning and for the PAD model.

The review screen follows the layout of Arthur's desktop tool: video and "Revoir segment" on the
left; segments table, transcription and PAD annotation on the right; "Précédent" and
"Valider & Suivant" to move through the segments.

## How clips are cut

| Mode | Use it for | How |
|---|---|---|
| Sur les silences (default) | Voice comms | One clip per speech burst between pauses (`ffmpeg silencedetect`), 0.15 s padding, long bursts split at 20 s |
| Longueur fixe | Continuous speech | Fixed windows, 10 s by default |
| Sous-titres JSON | Videos with captions | Same rule as `data/scripts/segment_audio.py` in the Esports-Event-to-Commentary-Generation-LoL repo: a caption ends where the next one starts. The caption text is shown to the reviewer as a reference |
| Aucun | Separated voices | No clips are cut; import the per-player clips afterwards (see below) |

Clips are 16 kHz mono WAV, the format Parakeet/NeMo expect. Clip times are aligned with the video
player, including when the audio track starts later than the video (the extracted audio is padded
to match), and they are cut with sample accuracy.

The silence threshold (-35 dB) and minimum pause (0.5 s) can be changed at upload. Raise the
threshold (e.g. -30) if game sound keeps clips from splitting, lower it if quiet speech is lost.

## Importing separated voices (one lane per player)

When another tool separates the players' voices, upload the game video with the cutting mode
**"Aucun"**, then import its output on the video page ("Importer les voix séparées"). Every clip
is placed under the video on its player's lane, at the exact time it happens in the game, and
the lanes scroll with playback.

**What the voice-separation step must produce:** one ZIP with the audio clips (WAV, FLAC, MP3,
anything ffmpeg reads, in any folders) and a `manifest.csv` at its root:

```csv
player,start,end,file,text
Enjawve,83.42,85.37,Enjawve/0001.wav,
Kaylem,00:01:24.10,00:01:26.00,Kaylem/0001.wav,
MelyaP,84.90,,MelyaP/0001.wav,optional reference transcript
```

| Column | Required | Meaning |
|---|---|---|
| `player` | yes | Player name. One lane is drawn per name; use the same spelling in every game |
| `start` | yes | Where the clip starts **in the original video**, in seconds (`83.42`) or `[hh:]mm:ss.ms` |
| `end` | no | Where it ends; if empty, `start` + the clip's audio length |
| `file` | yes | Path of the clip inside the ZIP |
| `text` | no | Reference transcript, shown to the reviewer |

`manifest.jsonl` (one `{"player", "start", "end", "file", "text"}` object per line) also works.
Clips of different players may overlap: people talk over each other. The times must be measured
from the start of the uploaded video, so a separation run on an extract has to add the
extract's offset. Rows are checked before anything is stored, and the error names the bad row.

The import can also run from a script with the API token:

```bash
curl -H "Authorization: Bearer $REVIEW_API_TOKEN" -F file=@separated.zip -F replace=true \
     https://<vm>/api/videos/<video_id>/import
```

`replace` deletes the video's existing clips first (for example the ones cut on silences);
without it the imported clips are added next to them.

**On the timeline:** click a clip to review it, click anywhere else to move the video there,
`+`/`−` or Ctrl + mouse wheel to zoom. With "Son : pistes séparées", the video is muted and each
player's separated clips play in sync with it; `M` mutes a lane and `S` plays one player only,
which is the quickest way to check the separation.

## Run it

Requires Python 3.10+ and `ffmpeg`/`ffprobe` on PATH.

```bash
pip install -r review_app/requirements.txt
export REVIEW_SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
python -m review_app.manage add-user oussama --role admin      # asks for a password (12+ characters)
python -m review_app.manage add-user arthur --role reviewer
uvicorn review_app.app:create_app --factory --host 127.0.0.1 --port 8000
```

Then open http://127.0.0.1:8000. Admins upload, transcribe, export and delete; reviewers review.

Run the tests with `pip install pytest httpx && pytest review_app/tests`.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `REVIEW_SECRET_KEY` | random per start | Signs the session cookie. Set a fixed value on the VM, otherwise every restart logs everyone out |
| `REVIEW_DATA_DIR` | `review_data` | SQLite database, user file, scratch space (and the files themselves with local storage) |
| `REVIEW_HTTPS_ONLY` | `false` | Set to `true` behind HTTPS so the cookie is never sent in clear |
| `REVIEW_MAX_UPLOAD_MB` | `4096` | Upload size limit |
| `REVIEW_STORAGE` | `local` | `local` or `azure` |
| `AZURE_STORAGE_ACCOUNT_URL` | | e.g. `https://<account>.blob.core.windows.net`, used with the VM's managed identity (recommended, needs `pip install azure-identity`) |
| `AZURE_STORAGE_CONNECTION_STRING` | | Alternative to the managed identity |
| `AZURE_STORAGE_CONTAINER` | `voicecomms-review` | Created private if missing |
| `REVIEW_TRANSCRIBER` | `none` | `none` (push transcripts through the API), `api` (call the fine-tuned Parakeet server) or `parakeet` (run a Hugging Face model in the app) |
| `REVIEW_ASR_URL` | `http://127.0.0.1:8800` | Parakeet server, with `REVIEW_TRANSCRIBER=api` |
| `REVIEW_ASR_API_MODEL` | `commentary` | `commentary` (casters, lowercase, no punctuation) or `gameaudio` (voice lines, punctuated), with `REVIEW_TRANSCRIBER=api` |
| `REVIEW_ASR_MODEL` | `nvidia/parakeet-tdt-0.6b-v3` | Model id or local path of the checkpoint, with `REVIEW_TRANSCRIBER=parakeet` |
| `REVIEW_API_TOKEN` | | Bearer token for scripts (empty disables token access) |

## Deploying on the Azure VM

1. **Storage.** Create a storage account and keep "Allow Blob anonymous access" **disabled**.
   Give the VM a system-assigned managed identity and the role *Storage Blob Data Contributor* on
   the account, then set `REVIEW_STORAGE=azure` and `AZURE_STORAGE_ACCOUNT_URL`. No key or SAS is
   stored on the VM, and no file ever gets a public URL: every video and clip is streamed through
   the app to a logged-in user.
2. **Service.** Run uvicorn with systemd, bound to `127.0.0.1`, with the variables above in an
   `EnvironmentFile` readable only by the service user.
3. **HTTPS.** Put nginx (or Caddy) in front with a TLS certificate and set `REVIEW_HTTPS_ONLY=true`.
   Keep the original `Host` header (`proxy_set_header Host $host;`), raise
   `client_max_body_size` to the upload limit, and disable request buffering for uploads
   (`proxy_request_buffering off;`). Alternatively keep the VM private and reach it through the
   company VPN or Azure Bastion.
4. **Network.** Open only 443 (or nothing, with VPN/Bastion) in the VM's network security group.

## Plugging in the model

**Through the fine-tuned Parakeet server** (recommended on the laptop): start it in its own
PowerShell window (`.\Start-Asr-Api.ps1` in the `FineTune` repo, ready at "Application startup
complete"), then run the app with `REVIEW_TRANSCRIBER=api`. "Transcrire" sends the clips 32 at a
time to `POST /transcribe/batch` on `REVIEW_ASR_URL`; audio goes from this app to the server on
127.0.0.1 and nowhere else. A clip the server cannot decode is left untranscribed (the file name is
logged, never the audio or the text); if the server is not started, the video shows that error.
The first call to a model not loaded yet takes about 30 s more.

**Inside the app**: install `torch` and `transformers`, set `REVIEW_TRANSCRIBER=parakeet` and point
`REVIEW_ASR_MODEL` at the fine-tuned checkpoint. The "Transcrire" button runs it on every clip
that has no transcript yet. To use NeMo directly instead of the Hugging Face pipeline, add a
`Transcriber` subclass in `transcriber.py`.

**From a separate GPU job** (keeps the web VM small):

```python
import requests
H = {'Authorization': f'Bearer {TOKEN}'}
for clip in requests.get(f'{URL}/api/videos/{VIDEO_ID}/clips', headers=H).json():
    if clip['model_text'] is None:
        wav = requests.get(f"{URL}/media/clips/{clip['id']}", headers=H).content
        text = my_model.transcribe(wav)
        requests.put(f"{URL}/api/clips/{clip['id']}/model-transcript", headers=H,
                     json={'text': text, 'model': 'parakeet-ft-v1'})
```

A new model output never overwrites a transcript a human already validated.

## Export

"Exporter (JSONL)" downloads one line per approved or corrected clip:

```json
{"audio_filepath": "videos/<id>/clips/clip_00012.wav", "duration": 1.95, "text": "I'm just pressing on the spell and then",
 "offset_in_video": 0.43, "end_in_video": 2.38, "video_id": "<id>", "source_file": "scrim_g1.mp4",
 "speaker": "Enjawve", "source": "separated", "pleasure": 4, "arousal": 4, "dominance": 4, "notes": null,
 "status": "approved", "model_text": "I'm just pressing on the spell and then", "reviewed_by": "arthur"}
```

`audio_filepath` is the path inside the storage container; copy the container (for example with
`azcopy copy`) and the manifest works as is for NeMo. Rejected clips are left out.

Each video's page also shows the model's **WER** against the human-validated transcripts, which
measures the ASR model on real team comms as the review goes.

## Security and GDPR

Voice recordings of players are personal data. The app is built so that:

- every page, API call and media file requires a login (sessions expire after 8 hours), with
  salted PBKDF2 password hashes, login throttling, admin/reviewer roles, and protection against
  cross-site requests and framing;
- files stay in a private container or on the VM disk and are only streamed through the app;
- an **audit log** (`GET /api/audit`, admins) records logins, uploads, reviews, exports and deletions;
- **"Supprimer"** erases a video, its audio, every clip and every annotation (right to erasure);
- nothing leaves the VM unless an admin exports it.

The rest is organisational and belongs in the GDPR section of the report: the players' consent
or the legal basis for processing, how long recordings are kept, and who holds an account.

## Layout

```
review_app/
├── app.py           # routes, background processing, media streaming
├── segmenter.py     # ffmpeg: audio extraction, silence/fixed/caption cutting
├── importer.py      # reads the ZIP + manifest of separated per-player clips
├── storage.py       # LocalStorage and AzureBlobStorage
├── transcriber.py   # pluggable ASR backends
├── auth.py, manage.py   # accounts and the admin CLI
├── db.py, metrics.py    # SQLite schema, WER
├── templates/, static/  # the web interface (French)
└── tests/
```
