import io
import re
import difflib
import streamlit as st
import pandas as pd
from collections import defaultdict

# --- FIXED: MOVED GOOGLE API IMPORTS TO THE GLOBAL TOP SCOPE ---
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# Page setup configuration
st.set_page_config(page_title="MCAT Converter", page_icon="🎵", layout="wide")

st.title("🎵 MCAT Converter")
st.markdown("""
1. **The app automatically pulls the most recent Composer Export and co-pub reference sheets** from your shared Google Drive folder layout.
2. **Prepare your MCAT excerpt** of works you want to import below.
3. 💡 *If you get an error message, ask AI (give it the link to this app along with the exact error message you receive).*
""")
st.markdown("---")

# Initialize session state variables to prevent download button resets
if 'processed' not in st.session_state:
    st.session_state.processed = False
if 'df_works' not in st.session_state:
    st.session_state.df_works = None
if 'df_alts' not in st.session_state:
    st.session_state.df_alts = None
if 'df_ip' not in st.session_state:
    st.session_state.df_ip = None
if 'df_qc' not in st.session_state:
    st.session_state.df_qc = None

# ==========================================
# AUTOMATED GOOGLE DRIVE FETCH ENGINE
# ==========================================
FOLDER_ID = "13-mxc5a2rIEly3ZMVSOoQjlDA4cmYx-D"

NAME_TO_CAE = {}
TOKEN_SET_TO_CAE = {}
EXPORT_NAMES_UPPER = []
COPUB_REFERENCE_DB = {}

def get_gdrive_service():
    """Authenticates using Streamlit's secure Secrets manager profile."""
    if "gdrive" not in st.secrets:
        st.error("Missing Google Drive API credentials. Please configure secrets in Streamlit Secrets.")
        return None
    
    creds_dict = dict(st.secrets["gdrive"])
    creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
    
    scopes = ['https://www.googleapis.com/auth/drive.readonly']
    creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return build('drive', 'v3', credentials=creds)

@st.cache_data(ttl=3600)
def load_reference_databases_from_drive():
    global NAME_TO_CAE, TOKEN_SET_TO_CAE, EXPORT_NAMES_UPPER, COPUB_REFERENCE_DB

    service = get_gdrive_service()
    if not service:
        return False
        
    try:
        results = service.files().list(
            q=f"'{FOLDER_ID}' in parents and trashed = false",
            fields="files(id, name)"
        ).execute()
        files = results.get('files', [])
        
        composer_file_id = None
        copub_file_id = None
        copub_is_xlsx = True
        
        for f in files:
            name_upper = f['name'].upper()
            if "COMPOSER EXPORT" in name_upper:
                composer_file_id = f['id']
            elif "CO-PUB REFERENCE" in name_upper or "CO-PUB" in name_upper:
                copub_file_id = f['id']
                copub_is_xlsx = f['name'].endswith('.xlsx')

        if composer_file_id:
            request = service.files().get_media(fileId=composer_file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            fh.seek(0)
            
            df_comp = pd.read_csv(fh, low_memory=False)
            NAME_TO_CAE.clear()
            TOKEN_SET_TO_CAE.clear()
            for _, row in df_comp.iterrows():
                name_orig = str(row['Name']).strip().upper()
                cae = clean_cae(row['CAE Number'])
                if name_orig and cae != "no match":
                    NAME_TO_CAE[name_orig] = cae
                    tokens = frozenset(name_orig.split())
                    if tokens:
                        TOKEN_SET_TO_CAE[tokens] = cae
            EXPORT_NAMES_UPPER = list(NAME_TO_CAE.keys())
            
        if copub_file_id:
            request = service.files().get_media(fileId=copub_file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            fh.seek(0)
            
            df_cp = pd.read_excel(fh, sheet_name=0) if copub_is_xlsx else pd.read_csv(fh)
            COPUB_REFERENCE_DB.clear()
            for _, row in df_cp.iterrows():
                if 'Writer Name' in df_cp.columns and 'Publishing Entity Name' in df_cp.columns:
                    w_clean = str(row['Writer Name']).split('(pka')[0].strip().upper()
                    COPUB_REFERENCE_DB[w_clean] = {
                        'pub_name': str(row['Publishing Entity Name']).strip(),
                        'pub_ipi': clean_cae(row.get('Publishing Entity IPI', 'no match'))
                    }
        return True
    except Exception as e:
        st.error(f"Failed to fetch master files from Google Drive folder location: {e}")
        return False


# ==========================================
# PARSING UTILITIES
# ==========================================
def clean_cae(val):
    if pd.isna(val):
        return "no match"
    s = str(val).strip()
    if s.endswith('.0'):
        s = s[:-2]
    if s.isdigit():
        s = s.zfill(9)
    return s

def query_database_for_cae(name_str):
    name_str = str(name_str).strip().upper()
    if not name_str or name_str in ["N/A", "UNKNOWN"]:
        return "no match"
    name_clean = name_str.replace('.', ' ').replace(',', ' ').replace('-', ' ')
    query_tokens = frozenset(name_clean.split())
    if not query_tokens:
        return "no match"
    if name_str in NAME_TO_CAE:
        return NAME_TO_CAE[name_str]
    if query_tokens in TOKEN_SET_TO_CAE:
        return TOKEN_SET_TO_CAE[query_tokens]
    for db_tokens, cae in TOKEN_SET_TO_CAE.items():
        if query_tokens.issubset(db_tokens) or db_tokens.issubset(query_tokens):
            if len(query_tokens.intersection(db_tokens)) >= 2:
                return cae
    matches = difflib.get_close_matches(name_str, EXPORT_NAMES_UPPER, n=1, cutoff=0.85)
    if matches:
        return NAME_TO_CAE[matches[0]]
    return "no match"

def clean_text(text):
    if pd.isna(text):
        return ""
    return str(text).strip()

def parse_title_and_alts(title_str):
    title_str = clean_text(title_str)
    match = re.search(r"\s*[\(\[]\s*\b(?:AKA|aka)\s+(.*?)\s*[\)\]]?$", title_str, re.IGNORECASE)
    if match:
        alt_content = match.group(1).strip('"\' ')
        clean_title = title_str[: match.start()].strip()
        clean_title = re.sub(r"[\s\(\),]+$", "", clean_title).strip()
        alts = []
        split_pattern = r'(?:"\s+and\s+["\']?|\'\s+and\s+["\']?|\s+and\s+|,)'
        for item in re.split(split_pattern, alt_content, flags=re.IGNORECASE):
            c_item = item.strip('"\' ').rstrip(")]").strip()
            if c_item:
                alts.append(c_item)
        return clean_title, alts
    return title_str, []

def extract_percentage(text_line, context=""):
    match = re.search(r"([\d.]+)\s*%", str(text_line))
    if match:
        clean_num = match.group(1).replace("..", ".")
        try:
            return round(float(clean_num), 2)
        except ValueError:
            return 0.0
    return 0.0

def parse_shares_field(shares_str):
    lines = [line.strip() for line in str(shares_str).split('\n') if line.strip()]
    direct_shares = []
    copub_shares = []
    for line in lines:
        if 'TOTAL' in line.upper():
            continue
        share_val = extract_percentage(line)
        if share_val == 0.0:
            continue
        if ' OBO ' in line.upper():
            match_obo = re.search(r'(.*?)\s+obo\s+(.*?)\s*-\s*([\d.]+)%', line, re.IGNORECASE)
            if match_obo:
                copub_shares.append({
                    'type': 'co-pub',
                    'payday_pub': match_obo.group(1).strip(),
                    'personal_pub': match_obo.group(2).strip(),
                    'share': share_val
                })
        else:
            match_direct = re.search(r'(.*?)\s*-\s*([\d.]+)%', line)
            if match_direct:
                direct_shares.append({
                    'type': 'direct',
                    'payday_pub': match_direct.group(1).strip(),
                    'share': share_val
                })
    return direct_shares, copub_shares

def parse_writers_block(block_str, title_context="", fallback_society="BMI"):
    if not block_str or clean_text(block_str).lower() in ["n/a", ""]:
        return []
    lines = [line.strip() for line in str(block_str).split("\n") if line.strip()]
    writers = []
    for line in lines:
        line_upper = line.upper()
        
        if "SOCAN" in line_upper: society = "SOCAN"
        elif "ASCAP" in line_upper: society = "ASCAP"
        elif "SESAC" in line_upper: society = "SESAC"
        elif "BMI" in line_upper: society = "BMI"
        else:
            if any(x in line_upper for x in ["SUISA", "GEMA", "PRS", "SACEM", "BUMA", "STEMRA", "TEOSTO", "TONO", "AKM", "SGAE", "SPA", "EUROPE"]):
                society = "EUROPE"
            else:
                society = "EUROPE" if fallback_society == "EUROPE" else "BMI"

        ipi_match = re.search(r"\b(\d{7,11})\b", line)
        ipi = ipi_match.group(1) if ipi_match else "no match"
        share = extract_percentage(line, context=title_context)

        name_part = line.split('(pka')[0].strip()
        share_match = re.search(r"([\d.]+)\s*%", name_part)
        if share_match:
            name_part = name_part[: share_match.start()]
        name = name_part.strip().strip("-").strip("\"'")

        if ipi == "no match" and name and EXPORT_NAMES_UPPER:
            ipi = query_database_for_cae(name)
        if name:
            writers.append({"name": name, "ipi": ipi, "share": share, "society": society})
    return writers

def parse_payday_writers(writer_str, ipi_str, title_context="", fallback_society="BMI"):
    raw_writers = parse_writers_block(writer_str, title_context=title_context, fallback_society=fallback_society)
    ipi_lines = str(ipi_str).split("\n") if not pd.isna(ipi_str) else []
    ipis_found = {}
    for line in ipi_lines:
        num_match = re.search(r"(0*\d{7,11})", line)
        if num_match:
            num = num_match.group(1)
            for rw in raw_writers:
                surname_token = rw["name"].split()[-1].lower()
                if surname_token in line.lower():
                    ipis_found[rw["name"]] = num

    single_num = re.match(r"^\s*(0*\d{7,11})\s*$", str(ipi_str).strip())
    final_writers = []
    for rw in raw_writers:
        w_ipi = ipis_found.get(rw["name"], "no match")
        if w_ipi == "no match" and single_num and len(raw_writers) == 1:
            w_ipi = single_num.group(1)
        elif w_ipi == "no match" and rw["ipi"] != "no match":
            w_ipi = rw["ipi"]
        final_writers.append({"name": rw["name"], "ipi": w_ipi, "share": rw["share"], "society": rw["society"]})
    return final_writers

def extrapolate_language(clean_title):
    title_lower = clean_title.lower()
    german_keywords = ["meiner", "halb", "was", "ich", "tipps", "bruder"]
    french_keywords = ["suis", "encore", "rouge", "noir", "nous", "rêve", "moi"]

    if any(k in title_lower for k in german_keywords):
        return "German"
    if any(k in title_lower for k in french_keywords):
        return "French"
    if "korean version" in title_lower:
        return "Korean"
    return "English"

def get_publisher_details(society_name):
    if society_name == "SOCAN": return "Payday Tunes Canada (SOCAN)", "1299996356"
    elif society_name == "ASCAP": return "Payday Tunes (ASCAP)", "1295254826"
    elif society_name == "SESAC": return "Payrec Music (SESAC)", "1297486002"
    elif society_name == "EUROPE": return "Payday Music Publishing Europe AG", "1298420430"
    return "Payday Empire Music", "1295942900"


# ==========================================
# BACKGROUND DATA TRIGGER EXECUTION
# ==========================================
db_connected = load_reference_databases_from_drive()

if db_connected:
    st.sidebar.success(f"Linked: Cloud databases active ({len(EXPORT_NAMES_UPPER)} writers / {len(COPUB_REFERENCE_DB)} co-pubs)")
else:
    st.sidebar.warning("Cloud databases offline. Check Streamlit deployment context configs.")

input_file = st.file_uploader("Upload your MCAT Excerpt File", type=["csv", "xlsx"])

if input_file:
    try:
        df = pd.read_csv(input_file) if input_file.name.endswith('.csv') else pd.read_excel(input_file, sheet_name=0)
        st.success(f"Loaded '{input_file.name}' successfully.")
    except Exception as e:
        st.error(f"Error loading file: {e}")
        df = None

    if df is not None:
        if st.button("🚀 Process Repertoire Layouts", type="primary"):
            
            # Smart Header Mapper Matrix
            col_map = {}
            for col in df.columns:
                c_norm = col.strip().upper().replace("\n", " ")
                if "SONG TITLE" in c_norm: col_map["title"] = col
                elif "PAYDAY SHARE" in c_norm: col_map["shares"] = col
                elif "PAYDAY WRITER" in c_norm and "CAE" not in c_norm: col_map["writers"] = col
                elif "CAE" in c_norm or "IPI" in c_norm: col_map["ipis"] = col
                elif "ADD" in c_norm and "WRITER" in c_norm: col_map["addl"] = col
                elif "ISRC" in c_norm: col_map["isrc"] = col
                elif "RELEASE DATE" in c_norm: col_map["release_date"] = col
                elif "LABEL" in c_norm: col_map["label"] = col
                elif "ARTIST" in c_norm: col_map["artist"] = col
                elif "CESSION" in c_norm: col_map["cession"] = col
                elif "AGREEMENT" in c_norm: col_map["agreement"] = col

            works_data, alts_data, ip_chain_data, qc_data = [], [], [], []

            for idx, row in df.iterrows():
                orig_title = clean_text(row[col_map["title"]]) if "title" in col_map else ""
                if not orig_title: continue

                clean_title, alts = parse_title_and_alts(orig_title)
                release_date = clean_text(row[col_map["release_date"]]) if "release_date" in col_map else ""
                label = clean_text(row[col_map["label"]]) if "label" in col_map else ""
                performers = clean_text(row[col_map["artist"]]).replace("\n", "; ").replace(",", ";") if "artist" in col_map else ""
                performers = "; ".join([p.strip() for p in performers.split(";") if p.strip()])
                isrcs = clean_text(row[col_map["isrc"]]).replace("\n", "; ").replace(",", ";") if "isrc" in col_map else ""
                isrcs = "; ".join([i.strip() for i in isrcs.split(";") if i.strip()])

                cession_val = clean_text(row[col_map["cession"]]).upper() if "cession" in col_map else ""
                notes_cession = "Mixed" if ("Y" in cession_val and "N" in cession_val) else ("BIEM" if "Y" in cession_val else "AA")
                
                lang = extrapolate_language(clean_title)

                works_data.append({
                    "ID": "", "Title": clean_title, "Composers": "", "Foreign ID": "", "Project ID": "",
                    "Party No": "", "Main Identifier": "", "ISWC": "", "Tunecode": "", "Copyright Date": release_date,
                    "Label Copy": f"(P) {release_date[-4:] if len(release_date)>4 else ''} {label}".strip(), "Priority Work": "False", 
                    "Production Library Work": "False", "Category": "Pop", "Language": lang, "Composite Type": "None", 
                    "No. of Composite Works": 0, "Work Version": "Original Work", "Arrangement Type": "Original", 
                    "Lyric Adaption": "Original", "Performers": performers, "Track ISRCs": isrcs, "Territories": "WW",
                    "Catalogue Groups": "PMPE;2026 - July New Works", "Aliases": "", "Notes": notes_cession