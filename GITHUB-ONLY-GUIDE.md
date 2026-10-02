# 🎬 GitHub-Only AI Video Pipeline — Super Easy Setup

**Tumar sob kaj: sheet e topic likhe `queued` boshano. Baki sob GitHub kore.**
Script → stock media → voice → render → upload → sheet update — 100% free, 100% automatic.

**Video type 5-ta (Sheet er `style` column e likhba):**

| style | Ki video | Avatar |
|---|---|---|
| (khali) | Documentary — Pixabay stock clips + voiceover | nai |
| `presenter` | Influencer / motivational speech (facecam) | girl-1 / boy-1 |
| `podcast` | Podcast style (avatar center, blur background) | girl-1 / boy-1 |
| `cartoon` | **Gopal Bhar type funny cartoon story** | cartoon-boy-1 / cartoon-girl-1 |
| `animation` | 3D/animated explainer (Pixabay 3D clips) | nai |

**Character/Avatar column (K) — 8-ta ready character:**

| Avatar | Char | Kokhon use korba |
|---|---|---|
| `girl-1` | meye presenter | motivation/lifestyle |
| `boy-1` | chele presenter | motivation |
| `cartoon-boy-1` | **Gopal Bhar type cartoon chele** | funny story |
| `cartoon-girl-1` | cartoon meye | funny story |
| `grandpa-1` | gnani dadu | golpo/history/wisdom |
| `robot-1` | robot | AI/tech (animation style) |
| `kid-boy-1` | bachcha chele | kids content |
| `bizman-1` | business man | success/business |

Khali rakhle voice dekhe auto beche ney. Voice chara dile ekdom thik matching voice nijei boshbe.
**Free limits:** repo public = Actions unlimited. Ek run e 5 video, prottek 3 ghonta.

---

## ☀️ Sokaler 10-minute setup (EKBAR, ekhone sesh)

### Step 1 — Sheet e Apps Script (2 minute, Google Cloud er dorkar NAI)

1. Tomar [Google Sheet](https://docs.google.com/spreadsheets/d/1HwesVw1hGORWq09wWUC_1e3oKgFES85E9vCzr8kklCY/edit) khulo
2. Menu: **Extensions → Apps Script**
3. Jeta ase sob **muchhe felo**, ei code paste koro: [apps-script.gs](https://base44.app/api/apps/6abeecc81ee9a618841d3dcb/files/mp/public/6abeecc81ee9a618841d3dcb/cbfe34c92_apps-script.gs) (TOKEN diye ready, kichu change korte hobe na)
4. **Deploy → New deployment** → type select: **Web app**
   - Execute as: **Me**
   - Who has access: **Anyone**
5. **Deploy → Authorize** (tomar Google account → **Advanced → Go to ... (unsafe) → Allow**)
6. **Web app URL ta copy koro** (`https://script.google.com/macros/s/..../exec`)

### Step 2 — Keys + URL dao (3 minute)

Ami je form pathiyechhi oi khane 3-ta boshao:
1. `GEMINI_API_KEY` — aistudio.google.com → Get API key (n8n e ja ase setai)
2. `PIXABAY_API_KEY` — pixabay.com/api/docs er key
3. `APPS_SCRIPT_URL` — Step 1 er shesh e je URL copy korle seta

Ami nijei GitHub secrets e boshaba (APPS_SCRIPT_TOKEN already boshano ache).

### Step 3 — pipeline.yml paste (1 minute)

1. [pipeline.yml](https://base44.app/api/apps/6abeecc81ee9a618841d3dcb/files/mp/public/6abeecc81ee9a618841d3dcb/8bfb3766a_pipeline.yml) khule **sob copy** koro
2. [GitHub repo](https://github.com/rafivai11233/video-renderer) → **Add file → Create new file**
3. Name: `.github/workflows/pipeline.yml` — paste → **Commit changes**

### Step 4 — First run (2 minute)

1. Repo → **Actions** tab → bam pashe **pipeline** → **Run workflow** → Run
2. Run e click kore log dekho:
   ```
   === next topic (row 2): ...
   Gemini: 12 scenes, mood=...
   plan.json written (style=..., voice=..., avatar=...)
   DONE: <job-id>
   ```
3. Sheet check koro: oi row e `done` + video link boshe geche ✅

**Ei 4 step sesh = setup SHESH. Ekhono kokhono kono setup korte hobe na.**

---

## 🔁 Daily routine (1 minute)

- Notun video chai? Sheet e notun row: **A**=topic, **B**=en/bn, **D**=queued, **J**=style, **K**=avatar
- Prottek 3 ghonta GitHub nije check kore, queue thakle video banaibe (max 5 per run)
- Video link **F** column e boshbe; `rendering` mane cholche, `failed` hole **I** column e reason
- Manual chai? Actions → pipeline → Run workflow

## 🕒 Sob somoy cholte

- Automatic cron prottek 3 ghonta (UTC) — queue khali thakle bas check kere chole jay
- ⚠️ Ekta mathay rakho: **60 din** repo te kono commit na thakle GitHub schedule off kore (age warning email dibe). Mashe ekbar choto ekta edit commit korlei safe.

## 🔧 Troubleshooting

| Symptom | Fix |
|---|---|
| `bad token` | Apps Script ta thik moto paste hoini — abar full code copy kore paste + **Deploy → Manage deployments → Edit → New version** |
| Script e `undefined` | Sheet er tab er naam thik `Topics` ache? Guide tab chara |
| `GEMINI_API_KEY` missing | Form ta bhora hoy nai — amake bolba, ami check korbo |
| Gemini 429 | Free daily limit sesh — kal nije theke hobe |
| `No queued topics` | Normal — queue khali, ekta row `queued` boshao |
| Cartoon video te avatar nai | K column khali + voice mile nai — K te `cartoon-boy-1` likho |
| Video 10 min er kom | Script e 12 scene × 170-200 katha thaka uchit — Gemini prompt automatic ache, thakle thik ashe |
| Ek video 2 bar render | n8n ar pipeline duitai chalu — ekta bondho koro |

## Copyright (sob free & safe)

Avatar (real + cartoon) amader AI-generated; music synthesized; stock = Pixabay license; voice = Edge TTS; font = Noto (OFL). YouTube e upload korar moto kono copyright jhamela nai.
