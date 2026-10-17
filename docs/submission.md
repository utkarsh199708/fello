# Submission checklist and message

Task 7.7. Owner: Utkarsh. This file holds no secrets.

## Before sending

1. **The soak (7.1).** `/runs` shows three nights with `trigger: schedule`: 2026-10-17, 2026-10-18 and 2026-10-19. Do not redeploy before recording.
2. **The Loom (7.6)** is recorded from [loom-script.md](loom-script.md), and no frame shows a password, a `.env` or a connection string.
3. **Push to GitHub.** `origin` is https://github.com/utkarsh199708/fello, and local `main` is 34 commits ahead of it, so run `git push origin main`. `.env` is ignored, and the brief is the first commit (c016419). Check that CI passes, and that the repo's visibility is what Fello asked for.
4. **The live site answers:** `https://136-83-131-193.sslip.io/health` returns `"status":"ok"` with the latest night.
5. **Read the shared password on the VM**, without printing it anywhere else:

   ```powershell
   & "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute ssh monday-list --zone asia-south1-a --project fello-monday-list --command "sudo grep '^BASIC_AUTH_PASSWORD=' /opt/monday-list/.env"
   ```

   Send it in a separate message from the links.

## The message

> **Monday list: Fello GTM AI take-home**
>
> - **Repo:** https://github.com/utkarsh199708/fello. The solution brief is the first commit ([docs/brief.md](brief.md)), written before any code.
> - **Live:** https://136-83-131-193.sslip.io. The username is ignored, and I'll send the password separately. Pick an organisation and a CSM on the first page.
> - **Loom:** \<Loom URL\>, about 11 minutes.
> - **Deck:** docs/deck.pptx in the repo, 9 slides.
>
> In short:
>
> - I built a ranked Monday list per CSM, with reasons that click through to the raw events, instead of a red/yellow/green score with auto-email. Why is in the brief and the README.
> - A nightly job runs at 02:00 UTC with nobody pressing a button. It opens tasks, escalates untouched ones near renewal, and logs every decision. The run history shows those unattended nights.
> - The model is a global LightGBM evaluated on a temporal holdout. It beats rules and the renewal calendar, and it ties logistic regression; the README has the tables.
> - The README's last section measures what breaks first at 10x.
>
> All data is synthetic.

## After the review

The VM bills about $21 a month. Once the review is over, stop it (or delete the project) from a terminal:

```powershell
& "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" compute instances stop monday-list --zone asia-south1-a --project fello-monday-list
```

This runs in PowerShell; `gcloud.cmd` is not on the PATH, so the full path is used. Stopping keeps the disk and the static IP, which still cost about $8 to $10 a month (an idle static IP is billed at a higher rate). Deleting the project stops all charges, and all data.
