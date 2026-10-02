# 🤖 GitHub-Only Pipeline — n8n ছাড়া 100% GitHub Actions

**এখন থেকে তুমি শুধু Sheet-এ প্রম্পট বদলাবে, GitHub নিজে থেকেই সব করবে:**
Sheet (queued টপিক) → Gemini স্ক্রিপ্ট → Pixabay ক্লিপ → Edge TTS → FFmpeg রেন্ডার → Release → Sheet-এ status + লিংক আপডেট।

- Schedule: **প্রতি ৩ ঘণ্টায় ক্রন** (স্বয়ংক্রিয়), সাথে manual "Run workflow" বাটন
- Repo public → Actions মিনিট **unlimited**, ২০০০ মিনিটের চিন্তা নেই
- ফাইনাল ভিডিও + থাম্বনেইল থাকে GitHub Release-এ (সরাসরি ডাউনলোড লিংক Sheet-এ বসে যায়)
- ভিডিও তৈরির প্রতিটি ফিচার রেডি: presenter/podcast অ্যাভাটার, মিউজিক, ক্যাপশন, থাম্বনেইল

⚠️ **একটা জরুরি কথা:** n8n workflow আর pipeline—দুটো একসাথে চালালে একই queued টপিক দুইবার রেন্ডার হবে। একটাকে বেছে নাও। GitHub-only ব্যবহার করলে n8n workflow-টি **Inactive/Deactivate** করে রাখো।

---

## রেডি হয়ে গেছে (আমি করে দিয়েছি)

- `main.py` repo-তে পুশ করা হয়েছে (commit b9d72a68) — পুরো পাইপলাইনের ব্রেইন
- লোকালি সেলফটেস্ট পাস করেছে: main.py → plan → render → চূড়ান্ত mp4 (অ্যাভাটার + ভয়েস + মিউজিক + ক্যাপশন সব)
- `render.py` আগের মতোই আছে — সব অ্যাডভান্সড ফিচার একই সাথে কাজ করে

## ধাপ ১ — Google Service Account (একবারই, ~১০ মিনিট)

Sheet পড়া/লেখার জন্য GitHub-কে Google-এ প্রবেশাধিকার দিতে হবে:

1. https://console.cloud.google.com → উপরে "Select a project" → **New Project** → নাম দাও `video-pipeline` → Create
2. বাম মেনু → **APIs & Services → Library** → **Google Sheets API** খুঁজে Enable করো (Drive API-ও লাগবে না, শুধু Sheets)
3. **APIs & Services → Credentials** → **+ Create Credentials → Service account** → নাম দাও `pipeline-bot` → Create → (Role লাগবে না) → Done
4. তৈরি হওয়া service account-এ ক্লিক করো → **Keys** tab → **Add key → Create new key → JSON** → ডাউনলোড হবে (যেমন `video-pipeline-xxxx.json`)
5. JSON ফাইলটি নোটপ্যাডে খুলে **সবটা কপি** করো
6. JSON-এর ভিতরে `"client_email": "pipeline-bot@....iam.gserviceaccount.com"` — এই ইমেইলটি কপি করো
7. তোমার [Google Sheet](https://docs.google.com/spreadsheets/d/1HwesVw1hGORWq09wWUC_1e3oKgFES85E9vCzr8kklCY/edit)-এ **Share** → সেই ইমেইল পেস্ট করো → **Editor** → Send

## ধাপ ২ — GitHub Secrets (৩টি, ~২ মিনিট)

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret নাম | মান |
|---|---|
| `GEMINI_API_KEY` | তোমার AI Studio কী (`AIza...`) |
| `PIXABAY_API_KEY` | তোমার Pixabay API কী |
| `GOOGLE_SA_JSON` | পুরো JSON ফাইলের কনটেন্ট (ধাপ ১-এর ৫ নম্বর কপি, `{` দিয়ে শুরু হওয়া) |

(SHEET_ID দরকার নেই — ডিফল্ট হিসেবেই তোমার Sheet ব্যবহার করা হয়েছে।)

## ধাপ ৩ — pipeline.yml পেস্ট করা (১ মিনিট, ব্রাউজারে)

আমার টোকেন দিয়ে workflow ফাইল পুশ করা যায় না (GitHub নিরাপত্তা নিয়ম), তাই একবার হাতে করতে হবে:

1. [pipeline.yml ডাউনলোড করো](https://base44.app/api/apps/6abeecc81ee9a618841d3dcb/files/mp/public/6abeecc81ee9a618841d3dcb/0d74ab08e_pipeline.yml) করে খোলো (অথবা নিচের কোড কপি করো)
2. GitHub repo → **Add file → Create new file**
3. ফাইলের নাম: `.github/workflows/pipeline.yml` (স্ল্যাশগুলো লিখলেই ফোল্ডার তৈরি হবে)
4. কনটেন্ট পেস্ট করো → **Commit changes**

```yaml
name: pipeline
run-name: auto-video ${{ github.run_id }}

on:
  schedule:
    - cron: "0 */3 * * *"   # প্রতি ৩ ঘণ্টা (UTC)
  workflow_dispatch: {}

concurrency:
  group: video-pipeline
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  make-video:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install ffmpeg, fonts, python libs
        run: |
          sudo apt-get update -qq
          sudo apt-get install -y -qq ffmpeg fontconfig fonts-noto-core fonts-noto-ui-core
          pip install -q edge-tts requests gspread google-auth
      - name: Run pipeline (একটি queued টপিক -> সম্পূর্ণ ভিডিও)
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          PIXABAY_API_KEY: ${{ secrets.PIXABAY_API_KEY }}
          GOOGLE_SA_JSON: ${{ secrets.GOOGLE_SA_JSON }}
          SHEET_ID: ${{ vars.SHEET_ID }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: python main.py
```

## ধাপ ৪ — প্রথম রান (টেস্ট)

1. Repo → **Actions** tab → বাম দিকে **pipeline** → **Run workflow** বাটন → Run
2. রান-এ ক্লিক করে লাইভ লগ দেখো:
   ```
   picked row 2: <তোমার টপিক>
   Gemini: 12 scenes, mood=energetic
   scene 1: 178 words, 2 clips
   ...
   plan.json written (style=documentary, voice=...)
   render.py: TTS + ffmpeg render starting...
   DONE: <job_id>
   video: https://github.com/rafivai11233/video-renderer/releases/...
   ```
3. Sheet-এ যাচাই করো: সেই সারিটির এখন `done` + ৩টি লিংক

## এরপর থেকে রুটিন (তোমার কাজ শুধু এটুকুই)

- **নতুন ভিডিও চাইলে:** Sheet-এ নতুন সারি — `topic` লিখে `status=queued` রেখে দাও (ভাষা B কলামে: en/bn, স্টাইল J কলামে: khali/presenter/podcast)
- প্রতি ৩ ঘণ্টায় GitHub নিজে থেকেই একটি queued টপিক তুলে ভিডিও বানাবে
- স্ট্যাটাস দেখবে Sheet-এ: `rendering` → `done` (ব্যর্থ হলে `failed` + `error` কলামে কারণ)
- ভিডিও পাবে release লিংক থেকে (Sheet-এই লিংক বসে থাকে)

## সমস্যা হলে (troubleshooting)

| উপসর্গ | সমাধান |
|---|---|
| Actions log: `GOOGLE_SA_JSON missing` | Secret-এর নাম ঠিক আছে? পুরো JSON `{...}` সহ পেস্ট করেছ? |
| `403 PERMISSION_DENIED` (sheet) | Sheet-টি SA ইমেইলের সাথে share করেছ? (ধাপ ১.৭) Editor দিয়ে |
| Gemini 429 / ব্যর্থ | Free daily limit শেষ — কাল আবার নিজে থেকে চলবে |
| `No queued topics` | স্বাভাবিক — কিউ খালি, একটি সারি `queued` যোগ করো |
| Release-এ কিছু নেই কিন্তু run ব্যর্থ হয়েছে | log-এর `Traceback` দেখো; Sheet-এর error কলামে কারণ লেখা আছে |
| দুইবার একই ভিডিও | n8n আর pipeline দুটোই চালু আছে — একটাকে বন্ধ করো |

## YouTube-এ আপলোড?

পরে যোগ করা যাবে (একই main.py-তে আপলোড ধাপ): YouTube Data API free tier 10k units/day ≈ 150 আপলোড/দিন। বললে বানিয়ে দেব।
