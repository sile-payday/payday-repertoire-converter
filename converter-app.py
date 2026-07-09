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
            cae_col = [c for c in df_comp.columns if 'CAE' in str(c).upper() or 'IPI' in str(c).upper() or 'IDENTIFIER' in str(c).upper()][0]
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
    name_str = re.sub(r"\s*\(\s*(BMI|ASCAP|SESAC|SOCAN|SUISA|GEMA|PRS|SACEM|BUMA|STEMRA|IPI|\d+)\s*\)", "", name_str, flags=re.IGNORECASE)
    name_str = re.sub(r"\b(BMI|ASCAP|SESAC|SOCAN|SUISA|GEMA|PRS|IPI)\b", "", name_str, flags=re.IGNORECASE)
    name_str =