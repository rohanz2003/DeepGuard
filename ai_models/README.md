# Local audio research baseline

This is a **research-only** NumPy logistic-regression baseline, not a trained
CNN/RNN or a production deepfake detector. The backend can load its safe JSON
artifact. No pickle, joblib, remote inference, or automatic model download is used.

## Prepare MP3 data without changing originals

Labels come from the two folders and are not independently verified:

```text
dataset/
├── real/  # original MP3/WAV files, optionally in nested folders
└── fake/  # original MP3/WAV files, optionally in nested folders
```

Run from the workspace root in PowerShell:

```powershell
backend\.venv\Scripts\python.exe -m pip install -r ai_models\requirements.txt -r ai_models\requirements-preparation.txt
backend\.venv\Scripts\python.exe -m ai_models.prepare --source dataset --output dataset/prepared --skip-invalid
```

The optional pinned `imageio-ffmpeg` wheel supplies a local decoder if FFmpeg
is not on PATH. You can instead use `--ffmpeg "C:/path/to/ffmpeg.exe"`.
No audio is uploaded to a third party. Source files are never renamed or deleted.

The separate prepared dataset contains mono, 16 kHz, signed-16-bit PCM WAVs
and a private `manifest.json` mapping hashed output names to originals. It
retains only unique decoded audio, rejects cross-label content conflicts, and
accepts durations of 1–300 seconds. It never pads short clips or silently clips
long recordings. `--skip-invalid` records rejects and continues; without it,
any rejected file prevents publication of usable training data.

Every output directory must be new. Reruns refuse even an empty existing output;
use a new name such as `dataset/prepared-v2`. Failed runs leave a failure manifest,
not a partial usable dataset. Review exclusions in that local manifest. Originals,
prepared audio, manifests, model artifacts, and detailed provenance are gitignored.

## Train, then evaluate the exact saved holdout

```powershell
backend\.venv\Scripts\python.exe -m ai_models.train --dataset dataset/prepared --output ai_models/artifacts/baseline.json --group-by filename --seed 1337 --test-size 0.2
backend\.venv\Scripts\python.exe -m ai_models.evaluate --model ai_models/artifacts/baseline.json --dataset dataset/prepared --output ai_models/artifacts/baseline-evaluation.json
backend\.venv\Scripts\python.exe -m ai_models.smoke --model ai_models/artifacts/baseline.json --dataset dataset/prepared
```

Existing model/report files require explicit `--overwrite`. Prefer new artifact
names for a new experiment. Train and held-out data must each contain both labels
and at least two independent groups per class must be available before splitting.

`--group-by filename` uses original manifest names for inferred speaker IDs,
clip/sentence/line/prompt IDs, and copy variants. These are grouping hints only;
filenames and text are **not model features**. Default `--group-by content` only
groups exact decoded duplicates. Related aliases are joined before deduplication.
Normalization is fitted on training rows only. Seed 1337 and fraction 0.2 are fixed
before testing; this pipeline does not tune thresholds using the holdout.

Artifact version 1.1 stores exact train/test membership, dataset fingerprints,
group IDs, UTC training time, label mapping, threshold, and preprocessing metadata.
Evaluation reuses this membership, not a new random split. Changes to the dataset,
manifest, or requested seed/fraction/grouping cause evaluation to fail. Old artifacts
without this provenance must be retrained. Reports contain aggregate measured
accuracy, precision/recall/F1, confusion matrix, ROC-AUC, Brier score, and measured
audio-read/preprocessing plus inference latency. No general accuracy is promised.

The smoke command uses Flask's test client, injected local identity, and an
ephemeral repository for two saved holdout files. It verifies inference and scan
create/read/delete **without cloud writes**; it is not a live Firebase login test.

## Preprocessing and score semantics

The existing 36-feature `log-spectrum-rms-zcr-v1` contract is unchanged:

- 32 log-power spectral-band means, 25 ms Hann frames, 10 ms hop, 512-point FFT;
- RMS mean and standard deviation;
- zero-crossing mean and standard deviation.

The WAV preprocessor supports PCM widths of 1/2/3/4 bytes, mixes channels to mono,
and linearly resamples to 16 kHz. Preparation uses the local FFmpeg resampler and
writes 16 kHz data, so no second resampling is needed for prepared WAVs.

Score `1` is the synthetic-audio direction and `0` the bonafide direction. It is
**uncalibrated**, not a reliable probability of fraud. The fixed evaluation cutoff
is 0.5. The UI additionally marks the 0.35–0.5 band as needing review; that band is
a heuristic, not a separately evaluated decision rule. Scam-language analysis is
still unavailable; no text classifier or call-blocking action is part of training.

## Enable for local research

In private `backend/.env`:

```dotenv
MODEL_ARTIFACT_PATH=../ai_models/artifacts/baseline.json
MODEL_THRESHOLD=0.5
```

Relative artifact paths resolve from `backend/`, independent of shell directory.
Restart Flask and inspect `/api/health`. Model readiness is separate from Firestore
readiness; scan history still requires working Firebase/Firestore configuration.
The web upload endpoint accepts **PCM WAV and MP3**. MP3 uploads are decoded by
the backend's local FFmpeg boundary and normalized to mono 16 kHz signed-16-bit
PCM WAV for inference. The local dataset preparation tool remains separate from
the API upload path.

## Limitations and checks

The supplied recordings' labels, source licenses, speakers, transcript IDs, and
generator identities have not been independently audited. Exact duplicates are
removed but near duplicates can remain. Filename grouping cannot guarantee unseen
speakers, generators, or text. Source/codec/language differences may dominate these
simple spectral features. A measured holdout result is not real-world validation.
Test on a separate, verified speaker/generator-disjoint corpus before deployment.

```powershell
backend\.venv\Scripts\python.exe -m unittest discover -s ai_models/tests -v
```
