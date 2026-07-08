import io
import re
import difflib
import streamlit as st
import pandas as pd
from collections import defaultdict

# Set up page configurations
st.set_page_config(page_title="Payday Repertoire Converter", page_icon="🎵", layout="wide")

st.title("🎵 Payday Repertoire Converter")
st.markdown("Upload your master delivery spreadsheet below to instantly generate your Curve ingestion files and audit balance sheets.")

# ==========================================
# CORE PARSING & FUZZY LOOKUP MODULES
# ==========================================
NAME_TO_CAE = {}
TOKEN_SET_TO_CAE = {}
EXPORT_NAMES_UPPER = []

def clean_cae(val):
    if pd.isna(val):
        return "no match"
    s = str(val).strip()
    if s.endswith('.0'):
        s = s[:-2]
    if s.isdigit():
        s = s.zfill(9)
    return s

def index_composer_database(uploaded_db_file):
    global NAME_TO_CAE, TOKEN_SET_TO_CAE, EXPORT_NAMES_UPPER
    NAME_TO_CAE.clear()
    TOKEN_SET_TO_CAE.clear()
    EXPORT_NAMES_UPPER.clear()
    
    if uploaded_db_file is None:
        return

    try:
        df_comp = pd.read_csv(uploaded_db_file)
        for _, row in df_comp.iterrows():
            name_orig = str(row['Name']).strip().upper()
            cae = clean_cae(row['CAE Number'])
            if name_orig and cae != "no match":
                NAME_TO_CAE[name_orig] = cae
                tokens = frozenset(name_orig.split())
                if tokens:
                    TOKEN_SET_TO_CAE[tokens] = cae
        EXPORT_NAMES_UPPER = list(NAME_TO_CAE.keys())
    except Exception as e:
        st.error(f"Failed to read Composer Export file: {e}")

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

def parse_writers_block(block_str, title_context=""):
    if not block_str or clean_text(block_str).lower() in ["n/a", ""]:
        return []

    lines = [line.strip() for line in str(block_str).split("\n") if line.strip()]
    writers = []

    for line in lines:
        line_upper = line.upper()
        if "SOCAN" in line_upper:
            society = "SOCAN"
        elif "ASCAP" in line_upper:
            society = "ASCAP"
        elif "SESAC" in line_upper:
            society = "SESAC"
        else:
            society = "BMI"

        ipi_match = re.search(r"\b(\d{7,11})\b", line)
        ipi = ipi_match.group(1) if ipi_match else "no match"

        share = extract_percentage(line, context=title_context)

        name_part = line
        share_match = re.search(r"([\d.]+)\s*%", line)
        if share_match:
            name_part = name_part[: share_match.start()]

        name_part = re.sub(r":\d+", "", name_part)
        name_part = re.sub(r"\s*-\s*$", "", name_part)
        name_part = re.sub(r"\s*\(\s*\d+\s*\)\s*", "", name_part)
        name_part = re.sub(r"\s*\(.*?\)\s*$", "", name_part)

        name = name_part.strip().strip("\"'")
        
        if ipi == "no match" and name and EXPORT_NAMES_UPPER:
            ipi = query_database_for_cae(name)

        if name:
            writers.append({"name": name, "ipi": ipi, "share": share, "society": society})
    return writers

def parse_payday_writers(writer_str, ipi_str, title_context=""):
    raw_writers = parse_writers_block(writer_str, title_context=title_context)
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


# ==========================================
# INTERFACE WIDGETS
# ==========================================
with st.sidebar:
    st.header("🗄️ Optional Reference Reference")
    db_file = st.file_uploader("Upload 'Composer Export' file to enable background IPI auto-matching", type=["csv"])
    if db_file:
        index_composer_database(db_file)
        st.success("Composer Export database linked successfully!")

input_file = st.file_uploader("Upload your Repertoire File (CSV or Excel formats supported)", type=["csv", "xlsx"])

if input_file:
    # Handle file reading safely over browser streams
    try:
        if input_file.name.endswith('.csv'):
            df = pd.read_csv(input_file)
        else:
            df = pd.read_excel(input_file, sheet_name=0)
        st.success(f"Successfully loaded '{input_file.name}' with {len(df)} lines of track records.")
    except Exception as e:
        st.error(f"Error loading file: {e}")
        df = None

    if df is not None:
        if st.button("🚀 Process Repertoire Layouts", type="primary"):
            works_data, alts_data, ip_chain_data, qc_data = [], [], [], []
            
            # Check for column structure layout compatibility
            required_cols = ["Song Title", "Payday Share", "Payday Writer"]
            missing_cols = [c for k in required_cols if (c := k) not in df.columns]
            
            if missing_cols:
                st.error(f"Column Mismatch! Missing required layout columns: {missing_cols}")
            else:
                # Dynamic index token mapping targets
                cession_col = "Cession Letter? (Y/N)" if "Cession Letter? (Y/N)" in df.columns else "Cession Letter?"
                
                for idx, row in df.iterrows():
                    orig_title = clean_text(row["Song Title"])
                    if not orig_title:
                        continue

                    clean_title, alts = parse_title_and_alts(orig_title)
                    release_date = clean_text(row["Release Date"]) if "Release Date" in df.columns else ""
                    label = clean_text(row["Label"]) if "Label" in df.columns else ""

                    performers = clean_text(row["Artist Name"]).replace("\n", "; ").replace(",", ";") if "Artist Name" in df.columns else ""
                    performers = "; ".join([p.strip() for p in performers.split(";") if p.strip()])

                    isrcs = clean_text(row["ISRC #"]).replace("\n", "; ").replace(",", ";") if "ISRC #" in df.columns else ""
                    isrcs = "; ".join([i.strip() for i in isrcs.split(";") if i.strip()])

                    year_match = re.search(r"(\d{4})", release_date)
                    year = year_match.group(1) if year_match else ""
                    label_copy = f"(P) {year} {label}".strip() if label or year else ""

                    cession_val = clean_text(row[cession_col]).upper() if cession_col in df.columns else ""
                    if "Y" in cession_val and "N" in cession_val:
                        notes_cession = "Mixed"
                    elif "Y" in cession_val:
                        notes_cession = "BIEM"
                    else:
                        notes_cession = "AA"

                    lang = extrapolate_language(clean_title)

                    # 1. Works Processing
                    works_data.append({
                        "ID": "", "Title": clean_title, "Composers": "", "Foreign ID": "", "Project ID": "",
                        "Party No": "", "Main Identifier": "", "ISWC": "", "Tunecode": "", "Copyright Date": release_date,
                        "Label Copy": label_copy, "Priority Work": "False", "Production Library Work": "False",
                        "Category": "Pop", "Language": lang, "Composite Type": "None", "No. of Composite Works": 0,
                        "Work Version": "Original Work", "Arrangement Type": "Original", "Lyric Adaption": "Original",
                        "Performers": performers, "Track ISRCs": isrcs, "Territories": "WW",
                        "Catalogue Groups": "PMPE;2026 - July New Works", "Aliases": "", "Notes": notes_cession,
                    })

                    # 2. Alternate Titles Processing
                    for alt in alts:
                        alts_data.append({
                            "Work ID": "", "Work Title": clean_title, "Work Main Identifier": "", "Work Tunecode": "",
                            "Alternate Title": alt, "Language": lang,
                        })

                    # 3. IP Chain Multi-PRO Calculation Block
                    payday_writers = parse_payday_writers(row["Payday Writer"], row.get("Payday Writers' CAE/IPI#", ""), title_context=clean_title)
                    addl_writers = parse_writers_block(row.get("Add'l Writer/Pub Info", ""), title_context=clean_title)

                    audit_mech_owned, audit_mech_collected = 0.0, 0.0
                    audit_perf_owned, audit_perf_collected = 0.0, 0.0

                    payday_groups = defaultdict(list)
                    for pw in payday_writers:
                        share_cell_clean = clean_text(row["Payday Share"]).upper()
                        is_european_repertoire = "EUROPE" in share_cell_clean or "SUISA" in share_cell_clean or "GEMA" in share_cell_clean
                        group_key = "EUROPE_MASTER" if is_european_repertoire else pw["society"]
                        payday_groups[group_key].append(pw)

                    for group_key, writers_in_group in payday_groups.items():
                        if is_european_repertoire:
                            pub_name = "Payday Music Publishing Europe AG"
                            pub_cae = "1298420430"
                        else:
                            if group_key == "SOCAN":
                                pub_name = "Payday Tunes Canada (SOCAN)"
                                pub_cae = "1299996356"
                            elif group_key == "ASCAP":
                                pub_name = "Payday Tunes (ASCAP)"
                                pub_cae = "1295254826"
                            elif group_key == "SESAC":
                                pub_name = "Payrec Music (SESAC)"
                                pub_cae = "1297486002"
                            else:
                                pub_name = "Payday Empire Music"
                                pub_cae = "1295942900"

                        group_share_total = sum(w["share"] for w in writers_in_group)
                        total_cents = int(round(group_share_total * 100))
                        pub_perf_cents = total_cents // 2 
                        writer_perf_total_cents = total_cents - pub_perf_cents 

                        formatted_pub_mech = round(total_cents / 100.0, 2)
                        formatted_pub_perf = round(pub_perf_cents / 100.0, 2)

                        audit_mech_owned += formatted_pub_mech
                        audit_mech_collected += formatted_pub_mech
                        audit_perf_owned += formatted_pub_perf
                        audit_perf_collected += formatted_pub_perf

                        ip_row_payday = {
                            "Work ID": "", "Work Title": clean_title, "Work Main Identifier": "", "Work Tunecode": "", "Territory": "WW",
                            "Participant 1 Type": "Publisher", "Participant 1 Name": pub_name,
                            "Participant 1 First Name": "", "Participant 1 Middle Name": "", "Participant 1 Surname": "",
                            "Participant 1 CAE Number": pub_cae, "Participant 1 Controlled": "True",
                            "Participant 1 Mechanical Owned": formatted_pub_mech, "Participant 1 Mechanical Collected": formatted_pub_mech,
                            "Participant 1 Performance Owned": formatted_pub_perf, "Participant 1 Performance Collected": formatted_pub_perf,
                            "Participant 1 Capacity": "Original Publisher",
                        }

                        num_writers = len(writers_in_group)
                        base_writer_cents = writer_perf_total_cents // num_writers
                        extra_cents_remainder = writer_perf_total_cents % num_writers

                        for p_idx, pw in enumerate(writers_in_group, start=2):
                            prefix = f"Participant {p_idx}"
                            allocated_cents = base_writer_cents + (1 if (p_idx - 2) < extra_cents_remainder else 0)
                            formatted_pw_perf = round(allocated_cents / 100.0, 2)

                            audit_perf_owned += formatted_pw_perf
                            audit_perf_collected += formatted_pw_perf

                            ip_row_payday.update({
                                f"{prefix} Type": "Composer", f"{prefix} Name": pw["name"], f"{prefix} First Name": "",
                                f"{prefix} Middle Name": "", f"{prefix} Surname": "", f"{prefix} CAE Number": pw["ipi"],
                                f"{prefix} Controlled": "True", f"{prefix} Mechanical Owned": 0.0, f"{prefix} Mechanical Collected": 0.0,
                                f"{prefix} Performance Owned": formatted_pw_perf, f"{prefix} Performance Collected": formatted_pw_perf,
                                f"{prefix} Capacity": "Lyrics and Music",
                            })
                        ip_chain_data.append(ip_row_payday)

                    if addl_writers:
                        ip_row_2 = {
                            "Work ID": "", "Work Title": clean_title, "Work Main Identifier": "", "Work Tunecode": "", "Territory": "WW",
                        }
                        for p_idx, aw in enumerate(addl_writers, start=1):
                            prefix = f"Participant {p_idx}"
                            formatted_aw_share = round(aw["share"], 2)

                            audit_mech_owned += formatted_aw_share
                            audit_mech_collected += formatted_aw_share
                            audit_perf_owned += formatted_aw_share
                            audit_perf_collected += formatted_aw_share

                            ip_row_2.update({
                                f"{prefix} Type": "Composer", f"{prefix} Name": aw["name"], f"{prefix} First Name": "",
                                f"{prefix} Middle Name": "", f"{prefix} Surname": "", f"{prefix} CAE Number": aw["ipi"],
                                f"{prefix} Controlled": "False", f"{prefix} Mechanical Owned": formatted_aw_share,
                                f"{prefix} Mechanical Collected": formatted_aw_share, f"{prefix} Performance Owned": formatted_aw_share,
                                f"{prefix} Performance Collected": formatted_aw_share, f"{prefix} Capacity": "Lyrics and Music",
                            })
                        ip_chain_data.append(ip_row_2)

                    payday_writer_names = "; ".join([pw["name"] for pw in payday_writers]) if payday_writers else "None"
                    qc_data.append({
                        "Work Title": clean_title,
                        "Repertoire Region": "EUROPE (PMPE)" if is_european_repertoire else "NORTH AMERICA (PMP)",
                        "Payday Writers": payday_writer_names,
                        "Total Mechanical Owned": round(audit_mech_owned, 2),
                        "Total Mechanical Collected": round(audit_mech_collected, 2),
                        "Total Performance Owned": round(audit_perf_owned, 2),
                        "Total Performance Collected": round(audit_perf_collected, 2)
                    })

                # --- RENDER BROWSERS EXPORT STREAMS ---
                df_works = pd.DataFrame(works_data)
                df_alts = pd.DataFrame(alts_data)
                df_ip = pd.DataFrame(ip_chain_data)
                df_qc = pd.DataFrame(qc_data)

                st.markdown("---")
                st.subheader("📥 Download Generated Sheets")
                
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.download_button("📋 Download Works Tab", data=df_works.to_csv(index=False).encode('utf-8'), file_name="Curve_Works_Tab.csv", mime="text/csv")
                with col2:
                    st.download_button("🔗 Download Alternate Titles", data=df_alts.to_csv(index=False).encode('utf-8'), file_name="Curve_Alternate_Titles_Tab.csv", mime="text/csv")
                with col3:
                    st.download_button("⛓️ Download IP Chain Tab", data=df_ip.to_csv(index=False).encode('utf-8'), file_name="Curve_IP_Chain_Tab.csv", mime="text/csv")
                with col4:
                    st.download_button("🔍 Download QC Audit Log", data=df_qc.to_csv(index=False).encode('utf-8'), file_name="Curve_Quality_Control.csv", mime="text/csv")

                st.subheader("📊 Quality Control Summary Preview")
                st.dataframe(df_qc, use_container_width=True)