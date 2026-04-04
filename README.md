# Movie List Maintainer

Fills in **Year**, **Director**, **Country**, and **Genre** in your Google Sheet movie list using the [OMDb API](https://www.omdbapi.com/). Searches by **Title**, and never touches your **Rank** or **Notes** columns.

Supports multiple worksheet tabs in a single spreadsheet.

---

## Sheet format

Your sheet must have a header row. Supported column names:

| Column | Behavior |
|--------|----------|
| **Rank** | Never modified |
| **Title** | Used to search OMDb |
| **Year** | Filled from OMDb |
| **Director** | Filled from OMDb |
| **Country** | Filled from OMDb |
| **Genre** | Filled from OMDb |
| **Notes** | Never modified |

---

## Setup

### 1. Get a free OMDb API key

1. Go to https://www.omdbapi.com/apikey.aspx
2. Choose the **Free** tier (1,000 requests/day)
3. Check your email and click the activation link

> **Note:** The free tier allows 1,000 requests/day. The script skips rows where all
> four fields are already filled, so after the initial run only newly added movies
> will count against the limit.

---

### 2. Google Cloud setup

#### 2a. Create a project

1. Go to https://console.cloud.google.com
2. Click the project dropdown at the top → **New Project**
3. Name it (e.g. `movie-list-maintainer`) → **Create**

#### 2b. Enable APIs

1. In the left sidebar go to **APIs & Services → Library**
2. Search for **Google Sheets API** → click it → **Enable**
3. Go back to the library, search for **Google Drive API** → **Enable**

#### 2c. Create a Service Account

1. Go to **APIs & Services → Credentials**
2. Click **Create Credentials → Service Account**
3. Give it any name (e.g. `sheet-updater`) → **Create and Continue** → **Done**
4. Click the service account you just created
5. Go to the **Keys** tab → **Add Key → Create new key → JSON → Create**
6. A `credentials.json` file will download — move it into this project folder

> `credentials.json` is listed in `.gitignore` and will never be committed to git.

#### 2d. Share your Google Sheet with the service account

1. Open the downloaded `credentials.json` and copy the `client_email` value
   (it looks like `sheet-updater@your-project.iam.gserviceaccount.com`)
2. Open your Google Sheet
3. Click **Share** (top right) and paste that email address
4. Give it **Editor** access → **Share**

---

### 3. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

### 4. Configure worksheet tabs

Open `main.py` and edit the `DEFAULT_WORKSHEETS` list to match your tab names:

```python
DEFAULT_WORKSHEETS = [
    "Movies",
    "Weird Movies",
    "Dudeist Movies",
    "Documentaries",
    "Horror/Halloween",
]
```

---

### 5. Run

```bash
export OMDB_API_KEY="your_omdb_key_here"
export SHEET_NAME="My Movie List"   # exact name of your Google Sheet
python main.py
```

The script will process each tab in order. For each one it will:
1. Skip movies where all four fields are already filled
2. Fetch OMDb data for everything else
3. Show a preview of every field it wants to change
4. Ask `[y/N]` before writing anything to that tab
