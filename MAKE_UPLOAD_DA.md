# Filupload fra Make til Audio Splitter 2.1.0

Denne opdatering tilføjer filupload. Din eksisterende opdeling efter filstørrelse og dine faste ventetider beholdes. Den gamle metode med downloadlink på `/split` fungerer stadig.

## 1. Læg koden på den eksisterende Render-service

Erstat `app.py` og `requirements.txt` i det repository, som Render bygger servicen fra. Deploy derefter den nye version. Den nye afhængighed `python-multipart==0.0.32` skal installeres under buildet, så en genstart af den gamle version er ikke nok.

Efter deploy skal `GET /` vise `"version": "2.1.0"`. `/docs` skal indeholde `POST /split-upload`.

## 2. Skift det relevante HTTP-modul i Make

| Felt | Værdi |
| --- | --- |
| URL | `https://audio-splitter-2-0.onrender.com/split-upload` |
| Method | `POST` |
| Body content type | `multipart/form-data (supports file uploads)` |
| Parse response | `Yes` |
| Timeout | Behold den eksisterende værdi, fx `300` |

Tilføj to elementer under **Body content**:

### Element 1: filen

| Felt | Værdi |
| --- | --- |
| Name | `file` |
| Field type | `File` |
| Data | Filens **Data** fra det modul, der leverer filen |
| File name | Filens **Filename / File name** fra samme modul |

For originalfilen fra OneDrive-modul 18 er felterne `{{18.data}}` og `{{18.filename}}`. Hvis du vil sende den konverterede lydfil, vælger du i stedet begge felter fra det aktuelle CloudConvert-modul.

### Element 2: længden på delene

| Felt | Værdi |
| --- | --- |
| Name | `segment_seconds` |
| Field type | `Text` |
| Value | `900` |

Send filen som et **File**-felt. Den skal ikke laves om til base64 eller sættes ind i et JSON-felt. Make opretter selv multipart-headeren; tilføj ikke en manuel `Content-Type: application/json`-header fra den tidligere forespørgsel.

## 3. Behold ventetiderne og resten af grenen

Når filen er uploadet og gemt, svarer serveren med HTTP 202 og de samme felter som før:

```json
{
  "job_id": "...",
  "status": "processing",
  "status_url": "https://audio-splitter-2-0.onrender.com/status/...",
  "retry_after_seconds": 30
}
```

Feltet `retry_after_seconds` er fortsat kun information; koden ændrer ikke dine Sleep-moduler. Lad dine eksisterende ventetider og det efterfølgende GET-kald til `status_url` stå. Iteratoren bruger stadig `chunks[]`, og download af hver MP3-del fungerer som før.

Konverteringen og opdelingen foregår i baggrunden efter svaret. MP3-delene bruger fortsat mono, 16 kHz, 32 kbit/s og den valgte `segment_seconds`.

## Filgrænse og fejl

`MAX_UPLOAD_BYTES` følger som standard den eksisterende `MAX_DOWNLOAD_BYTES` (normalt 2 GiB). Du kan sætte en separat miljøvariabel i Render, hvis uploadgrænsen skal være anderledes.

| Resultat | Betydning |
| --- | --- |
| HTTP 202 | Filen er modtaget og et job er oprettet. |
| HTTP 400 | Den uploadede fil er tom. |
| HTTP 413 | Filen overskrider uploadgrænsen. |
| HTTP 422 | Filfeltet mangler, eller `segment_seconds` er ugyldig. |
| `status: error` | Jobbet blev oprettet, men behandlingen fejlede; læs feltet `error`. |
