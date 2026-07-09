import io
import re
import difflib
import streamlit as st
import pandas as pd
from collections import defaultdict

# --- GOOGLE CORE CLIENT UTILITIES ---
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# Main UI layout setup config parameters
st.set_page_config(page_title="MCAT Converter", page_icon="🎵", layout="wide")

st.title("🎵 MCAT Converter")
st.markdown("""
1. **The app automatically pulls the most recent Composer Export and co-pub reference sheets** from your shared Google Drive folder layout.
2. **Prepare your MCAT excerpt** of works you want to import below.
3. 💡 *If you get an error message, ask AI (give it the link to this app along with the exact error message you receive).*
""")
st.markdown("---")

# Active browser data session state managers
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

# Shared Cloud Folder Directory Path
FOLDER_ID = "13-mxc5a2rIEly3ZMVSOoQjlDA4cmYx-D"

# --- DEFINE THE EXACT 125-COLUMN BLUEPRINT MANDATED BY CURVE ---
IP_CHAIN_HEADERS = ["Work ID", "Work Title", "Work Main Identifier", "Work Tunecode", "Territory"]
for i in range(1, 11):
    IP_CHAIN_HEADERS.extend([
        f"Participant {i} Type", f"Participant {i} Name", f"Participant {i} First Name",
        f"Participant {i} Middle Name", f"Participant {i} Surname", f"Participant {i} CAE Number",
        f"Participant {i} Controlled", f"Participant {i} Mechanical Owned", f"Participant {i} Mechanical Collected",
        f"Participant {i} Performance Owned", f"Participant {i} Performance Collected", f"Participant {i} Capacity"
    ])


def get_gdrive_service():
    """Builds a verified credential pass over Streamlit cloud secrets configuration."""
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
    """Fetches reference data from Google Drive and returns data structures to avoid caching traps."""
    name_to_cae = {}
    token_set_to_cae = {}
    export_names_upper = []
    copub_reference_db = {}

    service = get_gdrive_service()
    if not service:
        return name_to_cae, token_set_to_cae, export_names_upper, copub_reference_db, False
        
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
            # Find the CAE/IPI column identifier dynamically
            cae_col = [c for c in df_comp.columns if 'CAE' in c.upper() or 'IPI' in c.upper() or 'IDENTIFIER' in c.upper()][0]
            for _, row in df_comp.iterrows():
                name_orig = str(row['Name']).strip().upper()
                cae = clean_cae(row[cae_col])
                if name_orig and cae != "no match":
                    name_to_cae[name_orig] = cae
                    tokens = frozenset(name_orig.split())
                    if tokens:
                        token_set_to_cae[tokens] = cae
            export_names_upper = list(name_to_cae.keys())
            
        if copub_file_id:
            request = service.files().get_media(fileId=copub_file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            fh.seek(0)
            
            df_cp = pd.read_excel(fh, sheet_name=0) if copub_is_xlsx else pd.read_csv(fh)
            # Normalize column matching matrix to bypass space variations
            df_cp.columns = [str(c).strip().upper() for c in df_cp.columns]
            w_col = [c for c in df_cp.columns if "WRITER" in c][0]
            p_col = [c for c in df_cp.columns if "ENTITY" in c or "PUBLISHER" in c or "PUBLISHING" in c][0]
            ipi_col = [c for c in df_cp.columns if "IPI" in c or "CAE" in c][0]

            for _, row in df_cp.iterrows():
                w_clean = str(row[w_col]).split('(pka')[0].strip().upper()
                copub_reference_db[w_clean] = {
                    'pub_name': str(row[p_col]).strip(),
                    'pub_ipi': clean_cae(row[ipi_col])
                }
        return name_to_cae, token_set_to_cae, export_names_upper, copub_reference_db, True
    except Exception as e:
        st.error(f"Failed to fetch master files from Google Drive folder location: {e}")
        return name_to_cae, token_set_to_cae, export_names_upper, copub_reference_db, False


# ==========================================
# PARSING & SANITATION UTILITIES
# ==========================================
def clean_composer_name(name_str):
    """Strips parenthetical society markers, uncontrolled strings, and loose digits from names."""
    if pd.isna(name_str):
        return ""
    name_str = str(name_str).strip()
    # Remove parenthetical elements containing PRO names, numbers, or IPI strings
    name_str = re.sub(r"\s*\(\s*(BMI|ASCAP|SESAC|SOCAN|SUISA|GEMA|PRS|SACEM|BUMA|STEMRA|IPI|\d+)\s*\)", "", name_str, flags=re.IGNORECASE)
    # Remove loose words matching PRO banners
    name_str = re.sub(r"\b(BMI|ASCAP|SESAC|SOCAN|SUISA|GEMA|PRS|IPI)\b", "", name_str, flags=re.IGNORECASE)
    # Remove loose standalone numeric IPI fragments
    name_str = re.sub(r"\b\d{7,11}\b", "", name_str)
    # Clean loose punctuation trails left behind
    name_str = re.sub(r"[\s\-,/]+$", "", name_str).strip()
    name_str = re.sub(r"^[\s\-,/]+", "", name_str).strip()
    return name_str

def find_copub_match(personal_pub_raw, payday_writers, copub_db):
    """Resolves co-pub entity parameters flexibly across spelling/spacing gaps via intersection maps."""
    c_pub = clean_composer_name(personal_pub_raw).upper()
    c_pub_tokens = set(c_pub.split())
    
    # Check publisher string matching intersections
    for w_name, data in copub_db.items():
        ref_pub = str(data['pub_name']).upper()
        ref_pub_tokens = set(ref_pub.split())
        if len(c_pub_tokens.intersection(ref_pub_tokens)) >= 2 or ref_pub in c_pub or c_pub in ref_pub:
            return data['pub_ipi'], w_name
            
    # Fallback to matching standard writer surnames if the corporate tag differs
    for w in payday_writers:
        surname = w['name'].split()[-1].upper()
        for w_name, data in copub_db.items():
            if surname in w_name:
                return data['pub_ipi'], w_name
                
    return "no match", "Unknown"

def clean_cae(val):
    if pd.isna(val):
        return "no match"
    s = str(val).strip()
    if s.endswith('.0'):
        s = s[:-2]
    if s.isdigit():
        s = s.zfill(9)
    return s

def query_database_for_cae(name_str, name_to_cae, token_set_to_cae, export_names_upper):
    name_str = str(name_str).strip().upper()
    if not name_str or name_str in ["N/A", "UNKNOWN"]:
        return "no match"
    name_clean = name_str.replace('.', ' ').replace(',', ' ').replace('-', ' ')
    query_tokens = frozenset(name_clean.split())
    if not query_tokens:
        return "no match"
    if name_str in name_to_cae:
        return name_to_cae[name_str]
    if query_tokens in token_set_to_cae:
        return token_set_to_cae[query_tokens]
    for db_tokens, cae in token_set_to_cae.items():
        if query_tokens.issubset(db_tokens) or db_tokens.issubset(query_tokens):
            if len(query_tokens.intersection(db_tokens)) >= 2:
                return cae
    matches = difflib.get_close_matches(name_str, export_names_upper, n=1, cutoff=0.85)
    if matches:
        return name_to_cae[matches[0]]
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

def parse_writers_block(block_str, name_to_cae, token_set_to_cae, export_names_upper, title_context="", fallback_society="BMI"):
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
        
        # Enforce name sanitation on ingestion
        name = clean_composer_name(name_part)

        if ipi == "no match" and name and export_names_upper:
            ipi = query_database_for_cae(name, name_to_cae, token_set_to_cae, export_names_upper)
        if name:
            writers.append({"name": name, "ipi": ipi, "share": share, "society": society})
    return writers

def parse_payday_writers(writer_str, ipi_str, name_to_cae, token_set_to_cae, export_names_upper, title_context="", fallback_society="BMI"):
    raw_writers = parse_writers_block(writer_str, name_to_cae, token_set_to_cae, export_names_upper, title_context=title_context, fallback_society=fallback_society)
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
# BACKGROUND DATA AUTOMATED FETCH
# ==========================================
NAME_TO_CAE, TOKEN_SET_TO_CAE, EXPORT_NAMES_UPPER, COPUB_REFERENCE_DB, db_connected = load_reference_databases_from_drive()

if db_connected:
    st.sidebar.success(f"Linked: Cloud reference files active ({len(EXPORT_NAMES_UPPER)} writers / {len(COPUB_REFERENCE_DB)} co-pubs)")
else:
    st.sidebar.warning("Cloud databases offline. Check Streamlit API Secrets.")

# Custom delimiter catalogue tag string input widget (Tag 3)
custom_delivery_tag = st.text_input("📁 Enter Custom Catalogue Delivery Tag (Appended as 3rd delimiter value)", value="2026 - July New Works")

input_file = st.file_uploader("Upload your MCAT Excerpt File", type=["csv", "xlsx"])

if input_file:
    try:
        df = pd.read_csv(input_file) if input_file.name.endswith('.csv') else pd.read_excel(input_file, sheet_name=0)
        st.success(f"Loaded '{input_file.name}' with {len(df)} lines successfully.")
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
                elif "RELEASE DATE" in c_norm: col_map["release_date"] = col
                elif "LABEL" in c_norm: col_map["label"] = col
                elif "ARTIST" in c_norm: col_map["artist"] = col
                elif "CESSION" in c_norm: col_map["cession"] = col
                elif "AGREEMENT" in c_norm: col_map["agreement"] = col

            # Shortest-name priority resolution mapping logic for ISRC column isolation
            isrc_cols = [c for c in df.columns if "ISRC" in c.strip().upper()]
            if isrc_cols:
                isrc_cols.sort(key=len)
                col_map["isrc"] = isrc_cols[0]

            works_data, alts_data, ip_chain_data, qc_data = [], [], [], []

            for idx, row in df.iterrows():
                isrcs = ""
                
                orig_title = clean_text(row[col_map["title"]]) if "title" in col_map else ""
                if not orig_title: continue

                clean_title, alts = parse_title_and_alts(orig_title)
                release_date = clean_text(row[col_map["release_date"]]) if "release_date" in col_map else ""
                label = clean_text(row[col_map["label"]]) if "label" in col_map else ""
                performers = clean_text(row[col_map["artist"]]).replace("\n", "; ").replace(",", ";") if "artist" in col_map else ""
                performers = "; ".join([clean_composer_name(p) for p in performers.split(";") if p.strip()])
                
                if "isrc" in col_map:
                    raw_isrc_text = clean_text(row[col_map["isrc"]])
                    isrc_tokens = re.split(r"[\s,\n;]+", raw_isrc_text)
                    isrcs = ";".join([i.strip() for i in isrc_tokens if i.strip()])

                # --- EXTRACT STRUCTURAL METADATA ARRAYS ---
                raw_shares_text = clean_text(row[col_map["shares"]]) if "shares" in col_map else ""
                raw_writers_text = clean_text(row[col_map["writers"]]) if "writers" in col_map else ""
                raw_ipis_text = clean_text(row[col_map["ipis"]]) if "ipis" in col_map else ""
                raw_addl_text = clean_text(row[col_map["addl"]]) if "addl" in col_map else ""
                agreement_text = clean_text(row[col_map["agreement"]]).upper() if "agreement" in col_map else ""

                row_fallback = "EUROPE" if any(x in raw_shares_text.upper() for x in ["SUISA", "EUROPE", "GEMA"]) else "BMI"
                payday_writers = parse_payday_writers(raw_writers_text, raw_ipis_text, NAME_TO_CAE, TOKEN_SET_TO_CAE, EXPORT_NAMES_UPPER, title_context=clean_title, fallback_society=row_fallback)
                addl_writers = parse_writers_block(raw_addl_text, NAME_TO_CAE, TOKEN_SET_TO_CAE, EXPORT_NAMES_UPPER, title_context=clean_title)
                direct_shares, copub_shares = parse_shares_field(raw_shares_text)

                # --- CATALOGUE GROUPS THREE-TAG STRATIFICATION CONCATENATOR ---
                cession_val = clean_text(row[col_map["cession"]]).upper() if "cession" in col_map else ""
                if "Y" in cession_val and "N" in cession_val:
                    tag1 = "Mixed"
                elif "Y" in cession_val and "N" not in cession_val:
                    tag1 = "Non-AA"
                else:
                    tag1 = "AA"

                has_eu_entity = any(w["society"] == "EUROPE" for w in payday_writers)
                has_na_entity = any(w["society"] in ["ASCAP", "BMI", "SESAC", "SOCAN"] for w in payday_writers)