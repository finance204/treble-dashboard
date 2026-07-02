import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import urllib.parse
import stripe
import os
import re
import sys
import numpy as np
import html
import concurrent.futures
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv


LOCAL_PACKAGES_DIR = os.path.join(os.path.dirname(__file__), ".python_packages")

if os.path.isdir(LOCAL_PACKAGES_DIR) and LOCAL_PACKAGES_DIR not in sys.path:
    sys.path.insert(0, LOCAL_PACKAGES_DIR)


st.set_page_config(page_title="Collections Dashboard", layout="wide")
load_dotenv()


def get_secret_or_env(name, default=""):
    try:
        return st.secrets.get(name, os.getenv(name, default))
    except Exception:
        return os.getenv(name, default)


def is_enabled(value):
    return str(value).strip().lower() in ["1", "true", "yes", "y", "on"]


def get_user_info():
    try:
        return dict(st.user.to_dict())
    except Exception:
        try:
            return dict(st.user)
        except Exception:
            return {}


def get_logged_in_email():
    user_info = get_user_info()

    for key in ["email", "preferred_username", "upn"]:
        value = user_info.get(key, "")

        if value:
            return str(value).strip().lower()

    try:
        return str(st.user.get("email", "") or "").strip().lower()
    except Exception:
        return str(getattr(st.user, "email", "") or "").strip().lower()


def is_user_logged_in():
    return bool(get_logged_in_email())


def get_query_param(name, default=""):
    value = st.query_params.get(name, default)

    if isinstance(value, list):
        return value[0] if value else default

    return value


def has_valid_embed_token():
    expected_token = get_secret_or_env("DASHBOARD_EMBED_TOKEN", "")

    if not expected_token:
        return False

    provided_token = get_query_param("token", "")
    return str(provided_token) == str(expected_token)


def has_public_section_view():
    allowed_sections = [
        "aging",
        "invoice-volume",
        "stripe-payments",
        "brazil-finance"
    ]

    return get_query_param("section", "").strip().lower() in allowed_sections


def require_password_login():
    expected_password = get_secret_or_env("DASHBOARD_PASSWORD", "")

    if not expected_password:
        st.error("Dashboard password is not configured.")
        st.stop()

    if st.session_state.get("dashboard_password_ok"):
        return

    st.title("Treble Dashboard")
    st.caption("Enter the Treble dashboard password to continue.")

    password = st.text_input(
        "Password",
        type="password",
        label_visibility="collapsed"
    )

    if st.button("Enter"):
        if password == expected_password:
            st.session_state.dashboard_password_ok = True
            st.rerun()

        st.error("Incorrect password.")

    st.stop()


def require_google_login():
    allowed_domain = get_secret_or_env(
        "DASHBOARD_ALLOWED_EMAIL_DOMAIN",
        "treble.ai"
    ).strip().lower().lstrip("@")

    if not is_user_logged_in():
        st.title("Treble Dashboard")
        st.caption("Sign in with your Treble Google account to continue.")

        if st.button("Sign in with Google"):
            st.login()

        try:
            has_partial_session = bool(st.user)
        except Exception:
            has_partial_session = False

        if has_partial_session:
            st.caption(
                "If you already signed in and still see this screen, reset the session and try again."
            )

            if st.button("Reset sign-in"):
                st.logout()

        st.stop()

    user_email = get_logged_in_email()

    if not user_email.endswith(f"@{allowed_domain}"):
        st.title("Treble Dashboard")
        st.error("This dashboard is restricted to Treble team accounts.")
        st.caption(f"Signed in as: {user_email or 'unknown account'}")

        if st.button("Sign out"):
            st.logout()

        st.stop()

    with st.sidebar:
        st.caption(f"Signed in as {user_email}")

        if st.button("Sign out"):
            st.logout()
            st.stop()


def require_dashboard_access():
    auth_enabled = is_enabled(
        get_secret_or_env("DASHBOARD_AUTH_ENABLED", "false")
    )

    if not auth_enabled:
        return

    if has_valid_embed_token() or has_public_section_view():
        return

    auth_mode = get_secret_or_env(
        "DASHBOARD_AUTH_MODE",
        "password"
    ).strip().lower()

    if auth_mode == "google":
        require_google_login()
    else:
        require_password_login()


require_dashboard_access()

try:
    stripe.api_key = st.secrets["STRIPE_SECRET_KEY"]
except:
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")


# ------------------
# GLOBAL HELPERS
# ------------------
def google_sheet_url(sheet_id, gid=None, sheet_name=None):
    if sheet_name:
        encoded_sheet_name = urllib.parse.quote(sheet_name)
        return (
            f"https://docs.google.com/spreadsheets/d/{sheet_id}"
            f"/gviz/tq?tqx=out:csv&sheet={encoded_sheet_name}"
        )

    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


@st.cache_data(ttl=300)
def load_google_sheet(sheet_id, gid=None, sheet_name=None):
    try:
        return pd.read_csv(google_sheet_url(sheet_id, gid=gid, sheet_name=sheet_name))
    except Exception:
        return load_google_sheet_with_service_account(
            sheet_id,
            gid=gid,
            sheet_name=sheet_name
        )


def clean_money(value):
    if pd.isna(value):
        return 0.0

    value = str(value).strip()

    if value == "":
        return 0.0

    value = (
        value.replace("R$", "")
        .replace("$", "")
        .replace(",", "")
        .replace(" ", "")
    )

    negative = False

    if value.startswith("(") and value.endswith(")"):
        negative = True
        value = value.replace("(", "").replace(")", "")

    try:
        number = float(value)
    except:
        number = 0.0

    return -number if negative else number


def money_fmt(value, currency="USD"):
    symbol = "R$" if currency == "BRL" else "$"
    return f"{symbol}{value:,.0f}"


def normalize_text(value):
    if pd.isna(value):
        return ""

    return (
        str(value)
        .strip()
        .lower()
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ã", "a")
        .replace("ç", "c")
    )


def month_options_recent(source_df, sort_col, label_col):
    if len(source_df) == 0:
        return []

    return (
        source_df[[sort_col, label_col]]
        .drop_duplicates()
        .sort_values(sort_col, ascending=False)[label_col]
        .tolist()
    )


def year_options_recent(source_df, year_col):
    if len(source_df) == 0 or year_col not in source_df.columns:
        return []

    return sorted(
        source_df[year_col].dropna().unique().astype(int).tolist(),
        reverse=True
    )


# ------------------
# GOOGLE SHEET FIXED SOURCE
# ------------------
sheet_id = "114oEoIZLBWxnXbQlm5qcnmGrmT0WBnAJzpQ6Kvb3XIY"
gid = "112700383"
url = google_sheet_url(sheet_id, gid=gid)
COMMENTS_SHEET_ID = sheet_id
COMMENTS_WORKSHEET_NAME = "dashboard_comments"
COMMENTS_COLUMNS = [
    "Table",
    "Record Key",
    "Client",
    "Collection Status",
    "Comment",
    "Updated At"
]


@st.cache_data(ttl=300)
def load_data():
    return load_google_sheet(sheet_id, gid=gid)


def get_service_account_credentials(scopes):
    from google.oauth2.service_account import Credentials

    try:
        service_account_info = st.secrets.get("gcp_service_account")
    except Exception:
        service_account_info = None

    if service_account_info:
        return Credentials.from_service_account_info(
            service_account_info,
            scopes=scopes
        )

    credentials_path = os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        "credentials/google-service-account.json"
    )

    return Credentials.from_service_account_file(
        credentials_path,
        scopes=scopes
    )


def worksheet_to_dataframe(worksheet):
    values = worksheet.get_all_values()

    if not values:
        return pd.DataFrame()

    headers = values[0]
    rows = values[1:]

    return pd.DataFrame(rows, columns=headers)


def load_google_sheet_with_service_account(sheet_id, gid=None, sheet_name=None):
    import gspread

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly"
    ]

    creds = get_service_account_credentials(scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(sheet_id)

    if sheet_name:
        worksheet = spreadsheet.worksheet(sheet_name)
        return worksheet_to_dataframe(worksheet)

    if gid is not None:
        for worksheet in spreadsheet.worksheets():
            if str(worksheet.id) == str(gid):
                return worksheet_to_dataframe(worksheet)

        raise ValueError(f"Worksheet gid not found: {gid}")

    return worksheet_to_dataframe(spreadsheet.sheet1)


@st.cache_resource(show_spinner=False)
def get_dashboard_comments_client():
    import gspread

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly"
    ]

    creds = get_service_account_credentials(scopes)
    return gspread.authorize(creds)


def get_dashboard_comments_worksheet():
    client = get_dashboard_comments_client()
    spreadsheet = client.open_by_key(COMMENTS_SHEET_ID)

    try:
        worksheet = spreadsheet.worksheet(COMMENTS_WORKSHEET_NAME)
    except Exception:
        worksheet = spreadsheet.add_worksheet(
            title=COMMENTS_WORKSHEET_NAME,
            rows=1000,
            cols=len(COMMENTS_COLUMNS)
        )

    values = worksheet.get_all_values()

    if not values:
        worksheet.append_row(COMMENTS_COLUMNS)
    elif values[0] != COMMENTS_COLUMNS:
        worksheet.update("A1:F1", [COMMENTS_COLUMNS])

    return worksheet


@st.cache_data(ttl=60, show_spinner=False)
def load_dashboard_comments():
    try:
        timeout_seconds = float(
            get_secret_or_env("DASHBOARD_COMMENTS_TIMEOUT_SECONDS", "4")
        )

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            lambda: get_dashboard_comments_worksheet().get_all_records()
        )

        try:
            records = future.result(timeout=timeout_seconds)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        comments_df = pd.DataFrame(records)
    except concurrent.futures.TimeoutError:
        return pd.DataFrame(columns=COMMENTS_COLUMNS)
    except Exception:
        return pd.DataFrame(columns=COMMENTS_COLUMNS)

    for col in COMMENTS_COLUMNS:
        if col not in comments_df.columns:
            comments_df[col] = ""

    comments_df = comments_df[COMMENTS_COLUMNS].copy()

    for col in COMMENTS_COLUMNS:
        comments_df[col] = comments_df[col].fillna("").astype(str)

    return comments_df


def normalize_dashboard_text(value):
    if pd.isna(value):
        return ""

    value = str(value).strip()

    if value.lower() in ["none", "nan", "null"]:
        return ""

    return value


def add_comments_from_sheet(dataframe, table_name, key_column):
    comments_df = load_dashboard_comments()

    if len(comments_df) == 0:
        dataframe["Comments"] = ""
        if "Collection Status" in dataframe.columns:
            dataframe["Collection Status"] = dataframe[
                "Collection Status"
            ].map(normalize_dashboard_text)
        return apply_comment_overrides(dataframe, table_name, key_column)

    table_comments = comments_df[
        comments_df["Table"].astype(str) == str(table_name)
    ].copy()

    if len(table_comments) > 0:
        comment_map = table_comments.set_index("Record Key")["Comment"].to_dict()
        status_map = table_comments.set_index("Record Key")["Collection Status"].to_dict()

        dataframe["Comments"] = dataframe[key_column].astype(str).map(
            lambda x: comment_map.get(x, "")
        )

        if "Collection Status" in dataframe.columns:
            dataframe["Collection Status"] = dataframe[key_column].astype(str).map(
                lambda x: status_map.get(x, "")
            )
            dataframe["Collection Status"] = dataframe[
                "Collection Status"
            ].map(normalize_dashboard_text)
    else:
        dataframe["Comments"] = ""

        if "Collection Status" in dataframe.columns:
            dataframe["Collection Status"] = ""

    return apply_comment_overrides(dataframe, table_name, key_column)


def apply_comment_overrides(dataframe, table_name, key_column):
    overrides = st.session_state.get("dashboard_comment_overrides", {})

    if not overrides:
        return dataframe

    def get_override(record_key, field_name, default_value=""):
        override_key = f"{table_name}||{record_key}"
        return overrides.get(override_key, {}).get(field_name, default_value)

    dataframe["Comments"] = dataframe[key_column].astype(str).map(
        lambda x: get_override(x, "Comment", dataframe.loc[
            dataframe[key_column].astype(str) == x,
            "Comments"
        ].iloc[0])
    )

    if "Collection Status" in dataframe.columns:
        dataframe["Collection Status"] = dataframe[key_column].astype(str).map(
            lambda x: get_override(x, "Collection Status", dataframe.loc[
                dataframe[key_column].astype(str) == x,
                "Collection Status"
            ].iloc[0])
        )
        dataframe["Collection Status"] = dataframe[
            "Collection Status"
        ].map(normalize_dashboard_text)

    return dataframe


def remember_comment_overrides(table_name, rows):
    if "dashboard_comment_overrides" not in st.session_state:
        st.session_state.dashboard_comment_overrides = {}

    for row in rows:
        record_key = "" if pd.isna(row.get("record_key")) else str(row.get("record_key"))

        if not record_key:
            continue

        comment = "" if pd.isna(row.get("comment")) else str(row.get("comment"))
        collection_status = (
            ""
            if pd.isna(row.get("collection_status", ""))
            else normalize_dashboard_text(row.get("collection_status", ""))
        )

        st.session_state.dashboard_comment_overrides[
            f"{table_name}||{record_key}"
        ] = {
            "Comment": comment,
            "Collection Status": collection_status
        }


def save_sheet_comment(
    table_name,
    record_key,
    client_name,
    comment,
    collection_status=""
):
    record_key = "" if pd.isna(record_key) else str(record_key)
    client_name = "" if pd.isna(client_name) else str(client_name)
    comment = "" if pd.isna(comment) else str(comment)
    collection_status = (
        "" if pd.isna(collection_status) else normalize_dashboard_text(collection_status)
    )

    row_values = [
        str(table_name),
        record_key,
        client_name,
        collection_status,
        comment,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ]

    try:
        worksheet = get_dashboard_comments_worksheet()
        records = worksheet.get_all_records()

        target_row = None

        for idx, record in enumerate(records, start=2):
            if (
                str(record.get("Table", "")) == str(table_name) and
                str(record.get("Record Key", "")) == record_key
            ):
                target_row = idx
                break

        if target_row:
            worksheet.update(f"A{target_row}:F{target_row}", [row_values])
        else:
            worksheet.append_row(row_values)

        load_dashboard_comments.clear()

    except Exception as e:
        st.warning(f"Comment could not be saved to Google Sheets: {e}")


def save_sheet_comments_batch(table_name, rows):
    cleaned_rows = []

    for row in rows:
        record_key = "" if pd.isna(row.get("record_key")) else str(row.get("record_key"))
        client_name = "" if pd.isna(row.get("client_name")) else str(row.get("client_name"))
        comment = "" if pd.isna(row.get("comment")) else str(row.get("comment"))
        collection_status = (
            ""
            if pd.isna(row.get("collection_status", ""))
            else normalize_dashboard_text(row.get("collection_status", ""))
        )

        if not record_key:
            continue

        cleaned_rows.append({
            "Table": str(table_name),
            "Record Key": record_key,
            "Client": client_name,
            "Collection Status": collection_status,
            "Comment": comment
        })

    if not cleaned_rows:
        return

    remember_comment_overrides(
        table_name,
        [
            {
                "record_key": row["Record Key"],
                "comment": row["Comment"],
                "collection_status": row["Collection Status"]
            }
            for row in cleaned_rows
        ]
    )

    try:
        worksheet = get_dashboard_comments_worksheet()
        records = worksheet.get_all_records()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        existing_by_key = {}

        for idx, record in enumerate(records, start=2):
            key = (
                str(record.get("Table", "")),
                str(record.get("Record Key", ""))
            )
            existing_by_key[key] = (idx, record)

        updates = []
        appends = []

        for row in cleaned_rows:
            key = (row["Table"], row["Record Key"])
            existing = existing_by_key.get(key)

            if existing:
                target_row, existing_record = existing

                is_unchanged = (
                    str(existing_record.get("Client", "")) == row["Client"] and
                    str(existing_record.get("Collection Status", "")) == row["Collection Status"] and
                    str(existing_record.get("Comment", "")) == row["Comment"]
                )

                if is_unchanged:
                    continue

                updates.append({
                    "range": f"A{target_row}:F{target_row}",
                    "values": [[
                        row["Table"],
                        row["Record Key"],
                        row["Client"],
                        row["Collection Status"],
                        row["Comment"],
                        now
                    ]]
                })
            else:
                if not row["Comment"] and not row["Collection Status"]:
                    continue

                appends.append([
                    row["Table"],
                    row["Record Key"],
                    row["Client"],
                    row["Collection Status"],
                    row["Comment"],
                    now
                ])

        if updates:
            worksheet.batch_update(updates)

        if appends:
            worksheet.append_rows(appends)

        if updates or appends:
            load_dashboard_comments.clear()

    except Exception as e:
        st.warning(f"Comments could not be saved to Google Sheets: {e}")


def dashboard_comment_updated_at(row):
    updated_at = pd.to_datetime(
        row.get("Updated At", ""),
        errors="coerce"
    )

    if pd.isna(updated_at):
        return pd.Timestamp.min

    return updated_at


def latest_dashboard_comment_for_keys(comments_df, table_name, record_keys):
    if len(comments_df) == 0:
        return {"Comment": "", "Collection Status": ""}

    cleaned_keys = [
        clean_identity_value(record_key)
        for record_key in record_keys
        if clean_identity_value(record_key)
    ]

    if not cleaned_keys:
        return {"Comment": "", "Collection Status": ""}

    matches = comments_df[
        (comments_df["Table"].astype(str) == str(table_name)) &
        (comments_df["Record Key"].astype(str).isin(cleaned_keys))
    ].copy()

    if len(matches) == 0:
        return {"Comment": "", "Collection Status": ""}

    matches["_updated_at_sort"] = matches.apply(
        dashboard_comment_updated_at,
        axis=1
    )
    matches = matches.sort_values("_updated_at_sort", ascending=False)

    for _, row in matches.iterrows():
        comment = clean_identity_value(row.get("Comment", ""))
        collection_status = normalize_dashboard_text(
            row.get("Collection Status", "")
        )

        if comment or collection_status:
            return {
                "Comment": comment,
                "Collection Status": collection_status
            }

    return {"Comment": "", "Collection Status": ""}


def save_sheet_comment_aliases(table_name, rows):
    alias_rows = []
    seen_keys = set()

    for row in rows:
        record_key = clean_identity_value(row.get("record_key", ""))

        if not record_key or record_key in seen_keys:
            continue

        seen_keys.add(record_key)
        alias_rows.append(row)

    save_sheet_comments_batch(table_name, alias_rows)


def comment_alias_keys(row, primary_key="", customer_col="Customer name"):
    keys = [
        clean_identity_value(primary_key),
        clean_identity_value(row.get("HS ID", "")),
        clean_identity_value(row.get("Customer name", "")),
        clean_identity_value(row.get(customer_col, "")),
        clean_identity_value(row.get("HS Name", ""))
    ]

    alias_keys = []

    for key in keys:
        if key and key != "-" and key not in alias_keys:
            alias_keys.append(key)

    return alias_keys


def add_latest_comments_by_alias(
    dataframe,
    table_name,
    aliases_col="Comment Keys"
):
    comments_df = load_dashboard_comments()

    if len(comments_df) == 0 or aliases_col not in dataframe.columns:
        return dataframe

    def resolve_comment(row):
        latest = latest_dashboard_comment_for_keys(
            comments_df,
            table_name,
            row.get(aliases_col, [])
        )

        latest_comment = clean_identity_value(latest.get("Comment", ""))
        latest_status = normalize_dashboard_text(
            latest.get("Collection Status", "")
        )

        if latest_comment:
            row["Comments"] = latest_comment

        if "Collection Status" in dataframe.columns and latest_status:
            row["Collection Status"] = latest_status

        return row

    return dataframe.apply(resolve_comment, axis=1)


def editor_has_changes(editor_key):
    editor_state = st.session_state.get(editor_key, {})

    if not isinstance(editor_state, dict):
        return False

    return bool(
        editor_state.get("edited_rows") or
        editor_state.get("added_rows") or
        editor_state.get("deleted_rows")
    )


def get_selected_dataframe_rows(selection):
    if selection is None:
        return []

    try:
        rows = selection.selection.rows
        return list(rows) if rows is not None else []
    except AttributeError:
        pass

    if isinstance(selection, dict):
        selection_data = selection.get("selection", {})

        if isinstance(selection_data, dict):
            return list(selection_data.get("rows", []) or [])

    return []


def widget_key(*parts):
    raw_key = "||".join("" if pd.isna(part) else str(part) for part in parts)
    return re.sub(r"[^A-Za-z0-9_]+", "_", raw_key)[:220]


def render_comments_form(
    table_name,
    form_key,
    dataframe,
    record_col,
    client_col,
    display_cols,
    status_col=None,
    status_options=None,
    height=None
):
    rows_to_save = []

    with st.form(form_key):
        st.markdown(f"""
        <style>
        div[data-testid="stVerticalBlockBorderWrapper"]:has(#{form_key}-comments-anchor) {{
            max-height:{height or 420}px;
            overflow-y:auto;
        }}
        div[data-testid="stVerticalBlockBorderWrapper"]:has(#{form_key}-comments-anchor) > div:first-child {{
            position:sticky;
            top:0;
            z-index:10;
            background:#FFFFFF;
            padding-top:4px;
            border-bottom:1px solid #E5E7EB;
        }}
        div[data-testid="stVerticalBlockBorderWrapper"]:has(#{form_key}-comments-anchor) div[data-testid="stHorizontalBlock"]:has(strong) {{
            position:sticky;
            top:0;
            z-index:12;
            background:#FFFFFF;
            border-bottom:1px solid #E5E7EB;
            padding:4px 0 6px 0;
        }}
        div[data-testid="stVerticalBlockBorderWrapper"]:has(#{form_key}-comments-anchor) textarea {{
            min-height:38px !important;
            height:38px !important;
            font-size:13px !important;
            line-height:1.25 !important;
            padding:7px 9px !important;
        }}
        div[data-testid="stVerticalBlockBorderWrapper"]:has(#{form_key}-comments-anchor) textarea:focus {{
            min-height:128px !important;
            height:128px !important;
        }}
        div[data-testid="stVerticalBlockBorderWrapper"]:has(#{form_key}-comments-anchor) [data-testid="stSelectbox"] {{
            font-size:13px !important;
        }}
        </style>
        """, unsafe_allow_html=True)

        header_widths = [
            2.9 if col == "Comments" else 1.5 if col == client_col else 0.85
            for col in display_cols
        ]

        table_container = st.container(border=True)

        with table_container:
            st.markdown(
                f"<span id='{form_key}-comments-anchor'></span>",
                unsafe_allow_html=True
            )
            header_cols = st.columns(header_widths)

            for col_obj, col_name in zip(header_cols, display_cols):
                col_obj.markdown(f"**{col_name}**")

            for row_index, row in dataframe.reset_index(drop=True).iterrows():
                record_key = "" if pd.isna(row[record_col]) else str(row[record_col])
                client_name = "" if pd.isna(row[client_col]) else str(row[client_col])
                comment_value = ""
                status_value = ""
                row_cols = st.columns(header_widths)

                for col_obj, col_name in zip(row_cols, display_cols):
                    if col_name == "Comments":
                        comment_value = col_obj.text_area(
                            "Comments",
                            value="" if pd.isna(row.get("Comments", "")) else str(row.get("Comments", "")),
                            key=widget_key(form_key, record_key, "comment"),
                            label_visibility="collapsed",
                            height=38
                        )
                    elif status_col and col_name == status_col:
                        current_status = "" if pd.isna(row.get(status_col, "")) else str(row.get(status_col, ""))
                        options = status_options or [""]
                        status_index = options.index(current_status) if current_status in options else 0

                        status_value = col_obj.selectbox(
                            status_col,
                            options,
                            index=status_index,
                            key=widget_key(form_key, record_key, "status"),
                            label_visibility="collapsed"
                        )
                    else:
                        col_obj.write("" if pd.isna(row.get(col_name, "")) else row.get(col_name, ""))

                rows_to_save.append({
                    "record_key": record_key,
                    "client_name": client_name,
                    "comment": comment_value,
                    "collection_status": status_value
                })

        submitted = st.form_submit_button("Save Comments")

    return submitted, rows_to_save


def render_comment_picker_table(
    table_name,
    dataframe,
    record_col,
    client_col,
    column_order,
    column_config,
    disabled_cols,
    key,
    height=360,
    summary_cols=None,
    status_col=None,
    status_options=None
):
    if len(dataframe) == 0:
        st.info("No data available.")
        return

    if st.session_state.pop(f"{key}_comment_saved", False):
        st.success("Comment saved.")

    summary_cols = summary_cols or []

    @st.dialog(f"Edit Client Comment - {table_name}", width="large")
    def edit_comment_dialog(row_data):
        client_label = clean_identity_value(row_data.get(client_col, ""))
        record_key = clean_identity_value(row_data.get(record_col, ""))
        comment_keys = row_data.get("Comment Keys", [record_key])
        current_comment = clean_identity_value(row_data.get("Comments", ""))

        if not isinstance(comment_keys, list):
            comment_keys = [record_key]

        comment_keys = [
            key for key in comment_keys
            if clean_identity_value(key) and clean_identity_value(key) != "-"
        ]

        st.markdown("""
        <style>
        .comment-modal-title {
            font-size:20px;
            font-weight:800;
            color:#111827;
            margin-bottom:12px;
        }
        .comment-modal-summary {
            display:grid;
            grid-template-columns:repeat(auto-fit,minmax(110px,1fr));
            gap:8px;
            margin:8px 0 16px 0;
        }
        .comment-modal-card {
            border:1px solid #E5E7EB;
            border-radius:8px;
            background:#F8FAFC;
            padding:8px 10px;
            min-height:50px;
            overflow:hidden;
        }
        .comment-modal-card-label {
            color:#6B7280;
            font-size:11px;
            font-weight:650;
            line-height:1.1;
            margin-bottom:5px;
            white-space:nowrap;
            overflow:hidden;
            text-overflow:ellipsis;
        }
        .comment-modal-card-value {
            color:#111827;
            font-size:13px;
            font-weight:750;
            line-height:1.15;
            white-space:nowrap;
            overflow:hidden;
            text-overflow:ellipsis;
        }
        div[data-testid="stDialog"] textarea {
            min-height:190px !important;
            font-size:14px !important;
            line-height:1.32 !important;
        }
        div[data-testid="stDialog"] div[data-testid="stHorizontalBlock"] button {
            border-radius:8px !important;
            min-height:38px !important;
            padding:0 10px !important;
            font-weight:750 !important;
            white-space:nowrap !important;
            width:100% !important;
        }
        div[data-testid="stDialog"] div[data-testid="stHorizontalBlock"] > div:nth-child(1) button {
            background:#16A34A !important;
            border-color:#16A34A !important;
            color:#FFFFFF !important;
        }
        </style>
        """, unsafe_allow_html=True)

        st.markdown(
            f"<div class='comment-modal-title'>Comment - {html.escape(client_label)}</div>",
            unsafe_allow_html=True
        )

        if summary_cols:
            visible_summary_cols = [
                col for col in summary_cols
                if col in row_data and clean_identity_value(row_data.get(col, ""))
            ][:5]

            summary_cards = []

            for col_name in visible_summary_cols:
                value = clean_identity_value(row_data.get(col_name, ""))
                value = (
                    value
                    .replace("🔴", "")
                    .replace("🟠", "")
                    .replace("🟢", "")
                    .strip()
                )

                summary_cards.append(
                    f"""
                    <div class="comment-modal-card">
                        <div class="comment-modal-card-label">{html.escape(str(col_name))}</div>
                        <div class="comment-modal-card-value">{html.escape(value)}</div>
                    </div>
                    """
                )

            st.markdown(
                f"<div class='comment-modal-summary'>{''.join(summary_cards)}</div>",
                unsafe_allow_html=True
            )

        selected_status = ""

        if status_col:
            current_status = clean_identity_value(row_data.get(status_col, ""))
            options = status_options or [""]
            status_index = options.index(current_status) if current_status in options else 0
            selected_status = st.selectbox(
                status_col,
                options,
                index=status_index,
                key=widget_key(key, record_key, "status")
            )

        edited_comment = st.text_area(
            "Comment",
            value=current_comment,
            height=230,
            key=widget_key(key, record_key, "comment")
        )

        action_col1, action_col2 = st.columns([1.15, 6.85])

        if action_col1.button(
            "Save",
            type="primary",
            key=widget_key(key, record_key, "save"),
            width="stretch"
        ):
            save_sheet_comment_aliases(
                table_name,
                [
                    {
                        "record_key": alias_key,
                        "client_name": client_label,
                        "comment": edited_comment,
                        "collection_status": selected_status
                    }
                    for alias_key in comment_keys
                ]
            )

            st.session_state[f"{key}_comment_saved"] = True
            st.session_state[f"{key}_skip_dialog_once"] = True
            st.rerun()

    selection = st.dataframe(
        dataframe,
        key=key,
        width="stretch",
        height=height,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_order=column_order,
        column_config=column_config
    )

    selected_rows = get_selected_dataframe_rows(selection)

    if st.session_state.pop(f"{key}_skip_dialog_once", False):
        selected_rows = []

    previous_selected_rows = st.session_state.get(
        f"{key}_last_selected_rows",
        []
    )

    selection_changed = selected_rows != previous_selected_rows
    st.session_state[f"{key}_last_selected_rows"] = selected_rows

    if selected_rows and selection_changed:
        selected_row_index = selected_rows[0]
        edit_comment_dialog(
            dataframe.iloc[selected_row_index].to_dict()
        )


if st.sidebar.button("🔄 Refresh Data"):
    st.cache_data.clear()
    st.rerun()


df = load_data()

# ------------------
# CLEAN
# ------------------
df.columns = df.columns.str.strip()

df["Due Date"] = pd.to_datetime(df["Due Date"], errors="coerce")
df["Creation Date"] = pd.to_datetime(df["Creation Date"], errors="coerce")

df["Status"] = (
    df["Status"]
    .astype(str)
    .str.strip()
    .str.lower()
)

df["Currency"] = (
    df["Currency"]
    .astype(str)
    .str.strip()
    .str.upper()
)

df = df[df["Status"].isin(["open", "paid"])].copy()

df["Amount Fixed (USD)"] = (
    df["Amount Fixed (USD)"]
    .astype(str)
    .str.replace("$", "", regex=False)
    .str.replace(",", "", regex=False)
    .str.strip()
)

df["Amount Fixed (USD)"] = pd.to_numeric(
    df["Amount Fixed (USD)"],
    errors="coerce"
).fillna(0)


def first_existing_column(dataframe, names, position=None):
    for name in names:
        if name in dataframe.columns:
            return name

    if position is not None and len(dataframe.columns) > position:
        return dataframe.columns[position]

    return None


def clean_identity_value(value):
    if pd.isna(value):
        return ""

    value = str(value).strip()

    if value.lower() in ["nan", "none", "null"]:
        return ""

    return value


HS_ID_SOURCE_COL = first_existing_column(
    df,
    [
        "HS ID",
        "HS_ID",
        "HubSpot ID",
        "Hubspot ID",
        "HubSpot Company ID",
        "hs_id"
    ],
    position=21
)

HS_NAME_SOURCE_COL = first_existing_column(
    df,
    [
        "HS Name",
        "HS_NAME",
        "HubSpot Name",
        "Hubspot Name",
        "HubSpot Company Name",
        "hs_name"
    ],
    position=22
)

CUSTOMER_ID_SOURCE_COL = first_existing_column(
    df,
    [
        "customer_id",
        "Customer ID",
        "Customer Id",
        "Stripe Customer ID",
        "Stripe customer ID",
        "Customer"
    ]
)

df["HS ID"] = (
    df[HS_ID_SOURCE_COL].map(clean_identity_value)
    if HS_ID_SOURCE_COL else ""
)

df["HS Name"] = (
    df[HS_NAME_SOURCE_COL].map(clean_identity_value)
    if HS_NAME_SOURCE_COL else ""
)

df["Customer Key"] = (
    df[CUSTOMER_ID_SOURCE_COL].map(clean_identity_value)
    if CUSTOMER_ID_SOURCE_COL else ""
)

df["Customer Name Key"] = df["Customer name"].map(clean_identity_value)
df["HS Name"] = np.where(
    df["HS Name"].astype(str).str.strip() != "",
    df["HS Name"],
    df["Customer Name Key"]
)


def build_hs_lookup(source_df):
    id_lookup = {}
    name_lookup = {}
    hs_name_id_lookup = {}

    identity_df = source_df[
        [
            "Customer Key",
            "Customer Name Key",
            "HS ID",
            "HS Name"
        ]
    ].copy()

    identity_df = identity_df.drop_duplicates()

    for _, row in identity_df.iterrows():
        identity = {
            "HS ID": clean_identity_value(row.get("HS ID", "")),
            "HS Name": clean_identity_value(row.get("HS Name", ""))
        }

        customer_key = clean_identity_value(row.get("Customer Key", ""))
        customer_name_key = clean_identity_value(row.get("Customer Name Key", ""))

        if customer_key and customer_key not in id_lookup:
            id_lookup[customer_key] = identity

        if customer_name_key and customer_name_key not in name_lookup:
            name_lookup[customer_name_key] = identity

        normalized_hs_name = normalize_text(identity["HS Name"])

        if (
            normalized_hs_name and
            identity["HS ID"] and
            normalized_hs_name not in hs_name_id_lookup
        ):
            hs_name_id_lookup[normalized_hs_name] = identity["HS ID"]

    return id_lookup, name_lookup, hs_name_id_lookup


(
    HS_LOOKUP_BY_CUSTOMER_ID,
    HS_LOOKUP_BY_CUSTOMER_NAME,
    HS_ID_BY_HS_NAME
) = build_hs_lookup(df)

df["HS ID"] = np.where(
    (df["HS ID"].astype(str).str.strip() == "") &
    (df["HS Name"].astype(str).str.strip() != ""),
    df["HS Name"].map(lambda value: HS_ID_BY_HS_NAME.get(normalize_text(value), "")),
    df["HS ID"]
)


def add_hs_identity(dataframe, customer_id_col=None, customer_name_col=None):
    dataframe = dataframe.copy()

    if "HS ID" not in dataframe.columns:
        dataframe["HS ID"] = ""

    if "HS Name" not in dataframe.columns:
        dataframe["HS Name"] = ""

    if len(dataframe) == 0:
        return dataframe

    def resolve_identity(row):
        current_hs_id = clean_identity_value(row.get("HS ID", ""))
        current_hs_name = clean_identity_value(row.get("HS Name", ""))

        if current_hs_id or current_hs_name:
            return pd.Series({
                "HS ID": current_hs_id,
                "HS Name": current_hs_name
            })

        if customer_id_col and customer_id_col in dataframe.columns:
            customer_id = clean_identity_value(row.get(customer_id_col, ""))
            identity = HS_LOOKUP_BY_CUSTOMER_ID.get(customer_id)

            if identity:
                return pd.Series(identity)

        if customer_name_col and customer_name_col in dataframe.columns:
            customer_name = clean_identity_value(row.get(customer_name_col, ""))
            identity = HS_LOOKUP_BY_CUSTOMER_NAME.get(customer_name)

            if identity:
                return pd.Series(identity)

            return pd.Series({
                "HS ID": "",
                "HS Name": customer_name
            })

        return pd.Series({
            "HS ID": "",
            "HS Name": current_hs_name
        })

    dataframe[["HS ID", "HS Name"]] = dataframe.apply(
        resolve_identity,
        axis=1
    )

    dataframe["HS ID"] = dataframe["HS ID"].map(clean_identity_value)
    dataframe["HS Name"] = dataframe["HS Name"].map(clean_identity_value)

    dataframe["HS ID"] = np.where(
        (dataframe["HS ID"].astype(str).str.strip() == "") &
        (dataframe["HS Name"].astype(str).str.strip() != ""),
        dataframe["HS Name"].map(
            lambda value: HS_ID_BY_HS_NAME.get(normalize_text(value), "")
        ),
        dataframe["HS ID"]
    )

    return dataframe


def display_hs_value(value):
    value = clean_identity_value(value)
    return value if value else "-"


def load_saved_dashboard_number(table_name, record_key, default=0.0):
    comments_df = load_dashboard_comments()

    if len(comments_df) == 0:
        return default

    matches = comments_df[
        (comments_df["Table"].astype(str) == str(table_name)) &
        (comments_df["Record Key"].astype(str) == str(record_key))
    ].copy()

    if len(matches) == 0:
        return default

    return parse_flexible_number(matches.iloc[-1].get("Comment", default), default)


def parse_flexible_number(value, default=0.0):
    if pd.isna(value):
        return default

    value = str(value).strip()

    if not value:
        return default

    value = (
        value.replace("R$", "")
        .replace("$", "")
        .replace("USD", "")
        .replace("BRL", "")
        .replace(" ", "")
    )

    is_negative = False

    if value.startswith("(") and value.endswith(")"):
        is_negative = True
        value = value[1:-1]

    if value.startswith("-"):
        is_negative = True
        value = value[1:]

    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        comma_parts = value.split(",")

        if len(comma_parts[-1]) <= 2:
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif value.count(".") > 1:
        value = value.replace(".", "")

    try:
        parsed = float(value)
    except Exception:
        return default

    return -parsed if is_negative else parsed

# -----------------------------------
# FILTRO YEAR - MOST RECENT FIRST
# -----------------------------------
df["Year"] = df["Due Date"].dt.year.astype("Int64")
years = sorted(df["Year"].dropna().unique(), reverse=True)

selected_year = st.sidebar.selectbox(
    "Select Year",
    ["All"] + [int(y) for y in years]
)

# ------------------
# FILTRO MONEDA
# ------------------
currencies = sorted(df["Currency"].dropna().unique())

selected_currency = st.sidebar.selectbox(
    "Select Currency",
    ["All"] + list(currencies)
)

if selected_currency != "All":
    df = df[df["Currency"] == selected_currency].copy()

# ------------------
# DASHBOARD TABS
# ------------------
SECTION_LABELS = {
    "aging": "Aging Analysis",
    "invoice-volume": "Invoice Volume",
    "stripe-payments": "Stripe Payments",
    "brazil-finance": "Brazil Finance"
}

NOTION_BLOCK_LINKS = [
    ("aging-kpis", "Aging KPIs", "aging"),
    ("aging-past-due", "Aging Analysis - Past Due Invoices", "aging"),
    ("aging-clients-at-risk", "Clients at Risk", "aging"),
    ("aging-over-90", "Clients with Invoices Over 90 Days", "aging"),
    ("aging-collections-due-date", "Collections Performance by Due Date", "aging"),
    ("aging-monthly-collection", "Monthly Collection %", "aging"),
    ("aging-analyze-month", "Analyze Month", "aging"),
    ("invoice-overview", "Invoice Volume Performance", "invoice-volume"),
    ("invoice-monthly-detail", "Monthly Detail", "invoice-volume"),
    ("invoice-churn", "Monthly Billing Churn Cases", "invoice-volume"),
    ("invoice-credit-notes", "Credit Notes Issued", "invoice-volume"),
    ("invoice-credit-notes-detail", "Credit Notes Detail", "invoice-volume"),
    ("invoice-refunds", "Refunds Issued", "invoice-volume"),
    ("invoice-refunds-detail", "Refunds Detail", "invoice-volume"),
    ("stripe-aging", "Collections By Aging", "stripe-payments"),
    ("stripe-monthly-collections", "Monthly Collections", "stripe-payments"),
    ("stripe-payments-method", "Payments By Method", "stripe-payments"),
    ("stripe-brand-summary", "Payment Method / Card Brand Summary", "stripe-payments"),
    ("stripe-top-payments", "Top Payments This Week / Last Week", "stripe-payments"),
    ("stripe-success-rate", "Payment Success Rate", "stripe-payments"),
    ("stripe-failed-reasons", "Failed Payment Reasons", "stripe-payments"),
    ("stripe-fees", "Stripe Fees By Month", "stripe-payments"),
    ("stripe-top-fees", "Top 10 Stripe Fees", "stripe-payments"),
    ("brazil-income", "Itau Bank Payments - Income", "brazil-finance"),
    ("brazil-income-variation", "Itau Bank Payments Variation", "brazil-finance"),
    ("brazil-investment", "Investment", "brazil-finance"),
    ("brazil-costs", "Brazil Costs Evolution", "brazil-finance"),
    ("brazil-cost-ratio", "Costs as % of Income", "brazil-finance"),
    ("brazil-payment-detail", "Payment Detail", "brazil-finance"),
    ("brazil-taxes", "Taxes Paid By Month", "brazil-finance"),
    ("brazil-balance-input", "Balance and Investment Input", "brazil-finance"),
    ("brazil-payment-requests", "Payment Requests", "brazil-finance"),
    ("brazil-nfse", "NFSE Status", "brazil-finance")
]

VIEW_SECTION_BY_KEY = {
    view_key: section_key
    for view_key, _, section_key in NOTION_BLOCK_LINKS
}

section_param = get_query_param("section", "all")
active_section = str(section_param).strip().lower()
active_view = get_query_param("view", "").strip().lower()

if active_view and active_view in VIEW_SECTION_BY_KEY:
    active_section = VIEW_SECTION_BY_KEY[active_view]
elif active_view:
    active_view = ""

if active_section not in SECTION_LABELS:
    active_section = "all"

if not active_view:
    st.title("Collections Dashboard 🔥")

section_links = {
    label: f"?section={section_key}"
    for section_key, label in SECTION_LABELS.items()
}

with st.sidebar.expander("Notion section links"):
    for label, link in section_links.items():
        st.markdown(f"[{label}]({link})")

with st.sidebar.expander("Notion block links"):
    for view_key, label, section_key in NOTION_BLOCK_LINKS:
        st.markdown(f"[{label}](?section={section_key}&view={view_key})")


STREAMLIT_OUTPUT_METHODS = [
    "area_chart",
    "audio",
    "balloons",
    "bar_chart",
    "bokeh_chart",
    "button",
    "camera_input",
    "caption",
    "checkbox",
    "code",
    "color_picker",
    "data_editor",
    "dataframe",
    "date_input",
    "download_button",
    "error",
    "exception",
    "file_uploader",
    "form_submit_button",
    "graphviz_chart",
    "header",
    "html",
    "image",
    "info",
    "json",
    "latex",
    "line_chart",
    "map",
    "markdown",
    "metric",
    "multiselect",
    "number_input",
    "plotly_chart",
    "pydeck_chart",
    "radio",
    "scatter_chart",
    "select_slider",
    "selectbox",
    "slider",
    "snow",
    "subheader",
    "success",
    "table",
    "tabs",
    "text",
    "text_area",
    "text_input",
    "time_input",
    "title",
    "toast",
    "toggle",
    "vega_lite_chart",
    "video",
    "warning",
    "write"
]

STREAMLIT_LAYOUT_METHODS = [
    "columns",
    "container",
    "empty",
    "expander",
    "form",
    "popover"
]

_ORIGINAL_STREAMLIT_METHODS = {}
_NOTION_VIEW_STATE = {
    "rendering": not active_view,
    "started": False
}


class SilentStreamlitBlock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def __getattr__(self, name):
        def silent_method(*args, **kwargs):
            return silent_streamlit_return(name, args, kwargs)

        return silent_method


def silent_streamlit_return(method_name, args, kwargs):
    if method_name in ["button", "checkbox", "download_button", "form_submit_button", "toggle"]:
        return False

    if method_name == "data_editor":
        return args[0] if args else pd.DataFrame()

    if method_name == "selectbox":
        options = args[1] if len(args) > 1 else kwargs.get("options", [])
        index = kwargs.get("index", 0)
        options = list(options)
        return options[index] if len(options) > index else None

    if method_name == "radio":
        options = args[1] if len(args) > 1 else kwargs.get("options", [])
        index = kwargs.get("index", 0)
        options = list(options)
        return options[index] if len(options) > index else None

    if method_name == "multiselect":
        return kwargs.get("default", [])

    if method_name == "slider":
        return kwargs.get("value", args[2] if len(args) > 2 else None)

    if method_name == "select_slider":
        return kwargs.get("value", None)

    if method_name == "number_input":
        return kwargs.get("value", 0)

    if method_name in ["text_area", "text_input"]:
        return kwargs.get("value", "")

    if method_name in ["date_input", "time_input"]:
        return kwargs.get("value", None)

    if method_name == "file_uploader":
        return None

    return None


def guarded_streamlit_method(method_name):
    original_method = _ORIGINAL_STREAMLIT_METHODS[method_name]

    def wrapped(*args, **kwargs):
        if _NOTION_VIEW_STATE["rendering"]:
            return original_method(*args, **kwargs)

        return silent_streamlit_return(method_name, args, kwargs)

    return wrapped


def guarded_streamlit_layout(method_name):
    original_method = _ORIGINAL_STREAMLIT_METHODS[method_name]

    def wrapped(*args, **kwargs):
        if _NOTION_VIEW_STATE["rendering"]:
            return original_method(*args, **kwargs)

        if method_name == "columns":
            spec = args[0] if args else kwargs.get("spec", 1)
            count = len(spec) if isinstance(spec, (list, tuple)) else int(spec)
            return [SilentStreamlitBlock() for _ in range(count)]

        return SilentStreamlitBlock()

    return wrapped


def guarded_streamlit_dialog(*args, **kwargs):
    original_dialog = _ORIGINAL_STREAMLIT_METHODS["dialog"]

    if _NOTION_VIEW_STATE["rendering"]:
        return original_dialog(*args, **kwargs)

    def silent_decorator(function):
        return function

    return silent_decorator


def install_notion_view_renderer():
    if not active_view:
        return

    for method_name in STREAMLIT_OUTPUT_METHODS + STREAMLIT_LAYOUT_METHODS + ["dialog"]:
        if hasattr(st, method_name):
            _ORIGINAL_STREAMLIT_METHODS[method_name] = getattr(st, method_name)

    for method_name in STREAMLIT_OUTPUT_METHODS:
        if method_name in _ORIGINAL_STREAMLIT_METHODS:
            setattr(st, method_name, guarded_streamlit_method(method_name))

    for method_name in STREAMLIT_LAYOUT_METHODS:
        if method_name in _ORIGINAL_STREAMLIT_METHODS:
            setattr(st, method_name, guarded_streamlit_layout(method_name))

    if "dialog" in _ORIGINAL_STREAMLIT_METHODS:
        st.dialog = guarded_streamlit_dialog


def notion_anchor(view_key):
    if active_view:
        if _NOTION_VIEW_STATE["started"] and view_key != active_view:
            st.stop()

        _NOTION_VIEW_STATE["rendering"] = view_key == active_view

        if view_key == active_view:
            _NOTION_VIEW_STATE["started"] = True

    if not active_view:
        return

    markdown_method = _ORIGINAL_STREAMLIT_METHODS.get("markdown", st.markdown)

    if view_key == active_view:
        markdown_method(
            f"<div id='{view_key}' style='height:1px;'></div>",
            unsafe_allow_html=True
        )


def section_is_visible(section_key):
    return active_section == "all" or active_section == section_key


install_notion_view_renderer()


if active_view:
    components.html(
        f"""
        <script>
        const viewKey = {active_view!r};
        function scrollToView() {{
            const target = window.parent.document.getElementById(viewKey);
            if (target) {{
                target.scrollIntoView({{behavior: "instant", block: "start"}});
            }}
        }}
        setTimeout(scrollToView, 500);
        setTimeout(scrollToView, 1200);
        </script>
        """,
        height=0
    )
elif active_section != "all":
    components.html(
        """
        <script>
        function resetSectionScroll() {
            const doc = window.parent.document;
            doc.documentElement.scrollTop = 0;
            doc.body.scrollTop = 0;
            doc.querySelectorAll('[data-testid="stMain"], section, main, div').forEach((el) => {
                if (el.scrollTop) {
                    el.scrollTop = 0;
                }
            });
        }
        setTimeout(resetSectionScroll, 100);
        setTimeout(resetSectionScroll, 700);
        setTimeout(resetSectionScroll, 1500);
        </script>
        """,
        height=0
    )


if active_section == "all":
    tab1, tab2, tab3, tab4 = st.tabs([
        "Aging Analysis",
        "Invoice Volume",
        "Stripe Payments",
        "Brazil Finance"
    ])
else:
    st.caption(f"Section view: {SECTION_LABELS[active_section]}")

# ==================================================
# TAB 1 - AGING ANALYSIS
# ==================================================
if section_is_visible("aging"):
    if active_section == "all":
        tab_context = tab1
    else:
        tab_context = st.container()

    with tab_context:

        notion_anchor("aging-kpis")

        today = pd.Timestamp.today().normalize()

        open_portfolio_df = df[df["Status"] == "open"].copy()

        past_due_df = df[
            (df["Status"] == "open") &
            (df["Due Date"].notna()) &
            (df["Due Date"] < today)
        ].copy()

        total_invoiced = df[
            df["Status"].isin(["open", "paid"])
        ]["Amount Fixed (USD)"].sum()

        total_open_portfolio = open_portfolio_df["Amount Fixed (USD)"].sum()
        total_past_due = past_due_df["Amount Fixed (USD)"].sum()

        past_due_df["Days Past Due"] = (
            today - past_due_df["Due Date"]
        ).dt.days

        avg_days = int(past_due_df["Days Past Due"].mean()) if len(past_due_df) else 0
        median_days = int(past_due_df["Days Past Due"].median()) if len(past_due_df) else 0

        past_due_vs_open_pct = (
            total_past_due / total_open_portfolio * 100
        ) if total_open_portfolio > 0 else 0

        past_due_vs_invoiced_pct = (
            total_past_due / total_invoiced * 100
        ) if total_invoiced > 0 else 0

        col1, col2, col3, col4, col5, col6 = st.columns(6)

        def card(title, value, color):
            st.markdown(f"""
            <div style="
                background:#111827;
                padding:14px;
                border-radius:16px;
                border-left:6px solid {color};
                box-shadow:0 6px 18px rgba(0,0,0,.15);
                height:130px;
                overflow:hidden;
            ">
                <div style="
                    color:#9CA3AF;
                    font-size:12px;
                    font-weight:600;
                    white-space:normal;
                    line-height:1.2;
                ">
                    {title}
                </div>
                <div style="
                    color:white;
                    font-size:22px;
                    font-weight:700;
                    margin-top:16px;
                    white-space:nowrap;
                    line-height:1.1;
                ">
                    {value}
                </div>
            </div>
            """, unsafe_allow_html=True)

        def small_card(title, value, color, subtitle=""):
            subtitle_html = (
                f"<div style='color:#64748B;font-size:12px;font-weight:500;margin-top:7px;'>"
                f"{subtitle}"
                f"</div>"
                if subtitle else ""
            )

            st.markdown(f"""
            <div style="
                background:#FFFFFF;
                padding:14px 16px;
                border-radius:10px;
                border:1px solid #E5E7EB;
                border-left:5px solid {color};
                box-shadow:0 3px 12px rgba(15,23,42,.06);
                min-height:92px;
                display:flex;
                flex-direction:column;
                justify-content:center;
            ">
                <div style="
                    color:#64748B;
                    font-size:12px;
                    font-weight:650;
                    margin-bottom:8px;
                    line-height:1.2;
                ">
                    {title}
                </div>
                <div style="
                    color:#0F172A;
                    font-size:25px;
                    font-weight:800;
                    line-height:1;
                    white-space:nowrap;
                ">
                    {value}
                </div>
                {subtitle_html}
            </div>
            """, unsafe_allow_html=True)

        with col1:
            card("Total Open Billing", f"${total_open_portfolio:,.0f}", "#8b5cf6")

        with col2:
            card("Total Past Due", f"${total_past_due:,.0f}", "#ef4444")

        with col3:
            card("Past Due vs Open", f"{past_due_vs_open_pct:.1f}%", "#3b82f6")

        with col4:
            card("Past Due vs Invoiced", f"{past_due_vs_invoiced_pct:.1f}%", "#6366f1")

        with col5:
            card("Avg Days Past Due", f"{avg_days}", "#f59e0b")

        with col6:
            card("Median Days Past Due", f"{median_days}", "#10b981")

        st.markdown("<br>", unsafe_allow_html=True)

        notion_anchor("aging-past-due")
        st.subheader("📉 Aging Analysis - Past Due Invoices")

        order = [
            "1. <0",
            "2. 0-30",
            "3. 31-60",
            "4. 61-90",
            "5. 91-120",
            "6. 121-360",
            "7. >360"
        ]

        def classify_past_due_group(days):
            if pd.isna(days):
                return ""
            if days < 0:
                return "1. <0"
            if days <= 30:
                return "2. 0-30"
            if days <= 60:
                return "3. 31-60"
            if days <= 90:
                return "4. 61-90"
            if days <= 120:
                return "5. 91-120"
            if days <= 360:
                return "6. 121-360"
            return "7. >360"

        aging_df = df[df["Due Date"].notna()].copy()
        aging_df["Days Past Due"] = (
            today - aging_df["Due Date"]
        ).dt.days
        aging_df["Past Due Groups"] = aging_df[
            "Days Past Due"
        ].apply(classify_past_due_group)
        aging_df = aging_df[aging_df["Past Due Groups"] != ""].copy()

        total_group = aging_df.groupby(
            "Past Due Groups",
            as_index=False
        )["Amount Fixed (USD)"].sum()

        total_group.rename(
            columns={"Amount Fixed (USD)": "TotalInvoiced"},
            inplace=True
        )

        open_group = aging_df[
            aging_df["Status"] == "open"
        ]

        open_group = open_group.groupby(
            "Past Due Groups",
            as_index=False
        )["Amount Fixed (USD)"].sum()

        open_group.rename(
            columns={"Amount Fixed (USD)": "OpenAmount"},
            inplace=True
        )

        final_df = pd.merge(
            total_group,
            open_group,
            on="Past Due Groups",
            how="left"
        )

        final_df["OpenAmount"] = final_df["OpenAmount"].fillna(0)

        final_df["Percent"] = (
            final_df["OpenAmount"] /
            final_df["TotalInvoiced"]
        ) * 100

        final_df["Percent"] = final_df["Percent"].fillna(0)

        final_df["Past Due Groups"] = pd.Categorical(
            final_df["Past Due Groups"],
            categories=order,
            ordered=True
        )

        final_df = final_df.sort_values("Past Due Groups")

        fig3 = px.bar(
            final_df,
            x="Past Due Groups",
            y="OpenAmount",
            color_discrete_sequence=["#1E3A8A"]
        )

        fig3.update_traces(
            hovertemplate="%{x}<br>$%{y:,.0f}<extra></extra>",
            marker_line_color="#60A5FA",
            marker_line_width=1.2
        )

        fig3.add_scatter(
            x=final_df["Past Due Groups"],
            y=final_df["Percent"],
            mode="lines+markers",
            yaxis="y2",
            name="% Open",
            marker=dict(
                size=9,
                color="white",
                line=dict(
                    width=2.2,
                    color="#60A5FA"
                )
            ),
            line=dict(
                width=3.2,
                color="#60A5FA",
                shape="spline"
            )
        )

        aging_y_max = final_df["OpenAmount"].max()

        for _, row in final_df.iterrows():
            if pd.isna(row["Past Due Groups"]):
                continue

            fig3.add_annotation(
                x=row["Past Due Groups"],
                y=row["OpenAmount"],
                text=(
                    f"<b>${row['OpenAmount']:,.0f}</b><br>"
                    f"<span style='color:#F97316'>{row['Percent']:.0f}% open</span>"
                ),
                showarrow=False,
                yshift=34,
                align="center",
                font=dict(
                    size=12,
                    color="#334155",
                    family="Arial"
                ),
                bgcolor="rgba(255,255,255,0.93)",
                bordercolor="rgba(96,165,250,0.45)",
                borderwidth=1,
                borderpad=4
            )

        fig3.update_layout(
            height=420,
            plot_bgcolor="#F8FAFC",
            paper_bgcolor="rgba(0,0,0,0)",
            font=dict(
                family="Arial",
                size=13,
                color="#111827"
            ),
            xaxis_title="Days Past Due",
            yaxis_title="Amount USD",
            yaxis=dict(
                tickprefix="$",
                separatethousands=True,
                gridcolor="rgba(0,0,0,0.05)",
                zeroline=False,
                color="#6B7280",
                tickfont=dict(size=11),
                range=[
                    0,
                    aging_y_max * 1.28
                    if aging_y_max > 0
                    else 10
                ]
            ),
            xaxis=dict(
                showgrid=False,
                color="#6B7280",
                tickfont=dict(size=11)
            ),
            yaxis2=dict(
                title="%",
                overlaying="y",
                side="right",
                range=[0, 110],
                showgrid=False,
                color="#6B7280",
                tickfont=dict(size=11)
            ),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="right",
                x=1,
                font=dict(
                    color="#111827",
                    size=11
                )
            ),
            margin=dict(
                l=20,
                r=20,
                t=58,
                b=20
            ),
            hoverlabel=dict(
                bgcolor="#111827",
                font_size=14,
                font_family="Arial",
                font_color="white"
            )
        )

        st.plotly_chart(
            fig3,
            width="stretch",
            key="aging_past_due_chart"
        )

        # ==================================================
        # CLIENTS AT RISK
        # ==================================================
        notion_anchor("aging-clients-at-risk")
        st.markdown("#### 🚨 Clients at Risk")
        st.caption("Clients with open invoices between 31 and 90 days past due.")

        risk_base_df = df[
            (df["Status"] == "open") &
            (df["Due Date"].notna())
        ].copy()

        risk_base_df["Days Past Due"] = (
            today - risk_base_df["Due Date"]
        ).dt.days

        risk_df = risk_base_df[
            (risk_base_df["Days Past Due"] >= 31) &
            (risk_base_df["Days Past Due"] <= 90)
        ].copy()

        if len(risk_df) > 0:

            def classify_bucket(days):
                past_due_group = classify_past_due_group(days)

                if past_due_group == "3. 31-60":
                    return "31-60"
                if past_due_group == "4. 61-90":
                    return "61-90"
                return ""

            risk_df["Bucket"] = risk_df["Days Past Due"].apply(classify_bucket)
            risk_df = risk_df[risk_df["Bucket"] != ""].copy()

            def risk_client_key(row):
                return (
                    clean_identity_value(row.get("HS ID", "")) or
                    clean_identity_value(row.get("HS Name", "")) or
                    clean_identity_value(row.get("Customer name", ""))
                )

            def first_clean_identity(series):
                for item in series:
                    value = clean_identity_value(item)

                    if value:
                        return value

                return ""

            def collect_comment_keys(group):
                keys = []

                for _, row in group.iterrows():
                    for key in comment_alias_keys(
                        row,
                        primary_key=row.get("Risk Client Key", "")
                    ):
                        if key and key not in keys:
                            keys.append(key)

                return keys

            risk_df["Risk Client Key"] = risk_df.apply(
                risk_client_key,
                axis=1
            )

            pivot_risk = risk_df.pivot_table(
                index="Risk Client Key",
                columns="Bucket",
                values="Amount Fixed (USD)",
                aggfunc="sum",
                fill_value=0
            ).reset_index()

            risk_identity_df = risk_df.groupby(
                "Risk Client Key",
                as_index=False
            ).agg(
                **{
                    "Customer name": ("Customer name", first_clean_identity),
                    "HS ID": ("HS ID", first_clean_identity),
                    "HS Name": ("HS Name", first_clean_identity)
                }
            )

            risk_comment_keys = risk_df.groupby(
                "Risk Client Key"
            ).apply(collect_comment_keys).to_dict()

            pivot_risk = pivot_risk.merge(
                risk_identity_df,
                on="Risk Client Key",
                how="left"
            )

            ordered_buckets = [
                "31-60",
                "61-90"
            ]

            for col in ordered_buckets:
                if col not in pivot_risk.columns:
                    pivot_risk[col] = 0

            pivot_risk = pivot_risk[
                [
                    "Risk Client Key",
                    "Customer name",
                    "HS ID",
                    "HS Name"
                ] + ordered_buckets
            ]

            pivot_risk["Total Open"] = (
                pivot_risk["31-60"] +
                pivot_risk["61-90"]
            )

            def assign_risk(row):
                if row["61-90"] > 0:
                    return "🔴 High"
                if row["31-60"] > 0:
                    return "🟠 Medium"
                return "🟠 Medium"

            pivot_risk["Risk"] = pivot_risk.apply(assign_risk, axis=1)

            pivot_risk["Record Key"] = pivot_risk.apply(
                lambda row: (
                    clean_identity_value(row["HS ID"])
                    or clean_identity_value(row["HS Name"])
                    or clean_identity_value(row["Customer name"])
                ),
                axis=1
            )

            pivot_risk["Comment Keys"] = pivot_risk.apply(
                lambda row: risk_comment_keys.get(
                    clean_identity_value(row.get("Risk Client Key", "")),
                    comment_alias_keys(row, primary_key=row.get("Record Key", ""))
                ),
                axis=1
            )

            pivot_risk = add_comments_from_sheet(
                pivot_risk,
                "Clients at Risk",
                "Record Key"
            )

            risk_comments = load_dashboard_comments()

            if len(risk_comments) > 0:
                def resolve_latest_risk_comment(row):
                    latest_comment = latest_dashboard_comment_for_keys(
                        risk_comments,
                        "Clients at Risk",
                        row.get("Comment Keys", [])
                    ).get("Comment", "")

                    return (
                        latest_comment
                        if clean_identity_value(latest_comment)
                        else clean_identity_value(row.get("Comments", ""))
                    )

                pivot_risk["Comments"] = pivot_risk.apply(
                    resolve_latest_risk_comment,
                    axis=1
                )

            pivot_risk = pivot_risk.sort_values(
                "Total Open",
                ascending=False
            ).head(15)

            money_cols = [
                "31-60",
                "61-90",
                "Total Open"
            ]

            for col in money_cols:
                pivot_risk[col] = pivot_risk[col].map(
                    lambda x: f"${x:,.0f}"
                )

            pivot_risk["HS ID"] = pivot_risk["HS ID"].map(display_hs_value)
            pivot_risk["HS Name"] = pivot_risk["HS Name"].map(display_hs_value)

            risk_editor_df = pivot_risk.copy().reset_index(drop=True)

            if st.session_state.pop("risk_comment_saved", False):
                st.success("Comment saved.")

            @st.dialog("Edit Client Comment")
            def edit_risk_comment_dialog(row_data):
                client_label = clean_identity_value(row_data.get("HS Name", ""))
                record_key = clean_identity_value(row_data.get("Record Key", ""))
                comment_keys = row_data.get("Comment Keys", [])
                current_comment = clean_identity_value(row_data.get("Comments", ""))

                if not isinstance(comment_keys, list):
                    comment_keys = [record_key]

                comment_keys = [
                    key for key in comment_keys
                    if clean_identity_value(key) and clean_identity_value(key) != "-"
                ]

                st.markdown("""
                <style>
                .risk-modal-title {
                    font-size:22px;
                    font-weight:800;
                    color:#111827;
                    margin-bottom:12px;
                }
                .risk-modal-grid {
                    display:grid;
                    grid-template-columns:repeat(5,minmax(0,1fr));
                    gap:8px;
                    margin:10px 0 16px 0;
                }
                .risk-modal-pill {
                    border:1px solid #E5E7EB;
                    border-radius:8px;
                    background:#F8FAFC;
                    padding:8px 10px;
                    min-height:54px;
                }
                .risk-modal-label {
                    color:#64748B;
                    font-size:11px;
                    font-weight:700;
                    text-transform:uppercase;
                    line-height:1.1;
                    margin-bottom:5px;
                }
                .risk-modal-value {
                    color:#111827;
                    font-size:15px;
                    font-weight:750;
                    line-height:1.15;
                    overflow:hidden;
                    text-overflow:ellipsis;
                    white-space:nowrap;
                }
                div[data-testid="stDialog"] textarea {
                    min-height:220px !important;
                    font-size:15px !important;
                    line-height:1.35 !important;
                }
                div[data-testid="stDialog"] div[data-testid="stHorizontalBlock"] button {
                    border-radius:8px !important;
                    min-height:38px !important;
                    padding:0 10px !important;
                    font-weight:750 !important;
                    white-space:nowrap !important;
                    width:100% !important;
                }
                div[data-testid="stDialog"] div[data-testid="stHorizontalBlock"] > div:nth-child(1) button {
                    background:#16A34A !important;
                    border-color:#16A34A !important;
                    color:#FFFFFF !important;
                }
                div[data-testid="stDialog"] div[data-testid="stHorizontalBlock"] > div:nth-child(2) button {
                    background:#FFFFFF !important;
                    border-color:#DC2626 !important;
                    color:#DC2626 !important;
                }
                </style>
                """, unsafe_allow_html=True)

                st.markdown(
                    f"<div class='risk-modal-title'>Comment - {html.escape(client_label)}</div>",
                    unsafe_allow_html=True
                )

                risk_clean = str(row_data.get("Risk", "")).replace("🔴", "").replace("🟠", "").strip()

                st.markdown(
                    f"""
                    <div class="risk-modal-grid">
                        <div class="risk-modal-pill">
                            <div class="risk-modal-label">HS ID</div>
                            <div class="risk-modal-value">{html.escape(str(row_data.get("HS ID", "-")))}</div>
                        </div>
                        <div class="risk-modal-pill">
                            <div class="risk-modal-label">31-60</div>
                            <div class="risk-modal-value">{html.escape(str(row_data.get("31-60", "$0")))}</div>
                        </div>
                        <div class="risk-modal-pill">
                            <div class="risk-modal-label">61-90</div>
                            <div class="risk-modal-value">{html.escape(str(row_data.get("61-90", "$0")))}</div>
                        </div>
                        <div class="risk-modal-pill">
                            <div class="risk-modal-label">Total Open</div>
                            <div class="risk-modal-value">{html.escape(str(row_data.get("Total Open", "$0")))}</div>
                        </div>
                        <div class="risk-modal-pill">
                            <div class="risk-modal-label">Risk</div>
                            <div class="risk-modal-value">{html.escape(risk_clean)}</div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

                edited_comment = st.text_area(
                    "Comment",
                    value=current_comment,
                    height=230,
                    key=widget_key("risk_comment_modal", record_key)
                )

                action_col1, action_col2 = st.columns([1.15, 6.85])

                if action_col1.button(
                    "Save",
                    type="primary",
                    key=widget_key("save_risk_comment", record_key),
                    width="stretch"
                ):
                    save_sheet_comment_aliases(
                        "Clients at Risk",
                        [
                            {
                                "record_key": alias_key,
                                "client_name": client_label,
                                "comment": edited_comment,
                                "collection_status": ""
                            }
                            for alias_key in comment_keys
                        ]
                    )

                    st.session_state.risk_comment_saved = True
                    st.session_state.skip_risk_dialog_once = True
                    st.rerun()

            risk_display_df = risk_editor_df.copy()
            risk_table_height = 88 + (len(risk_display_df) * 46)
            risk_table_height = min(max(risk_table_height, 170), 330)

            risk_selection = st.dataframe(
                risk_display_df,
                key="risk_clients_comment_picker",
                width="stretch",
                height=risk_table_height,
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                column_order=[
                    "HS ID",
                    "HS Name",
                    "31-60",
                    "61-90",
                    "Total Open",
                    "Risk",
                    "Comments"
                ],
                column_config={
                    "HS ID": st.column_config.TextColumn(
                        "HS ID",
                        width="small"
                    ),
                    "HS Name": st.column_config.TextColumn(
                        "HS Name",
                        width="medium"
                    ),
                    "31-60": st.column_config.TextColumn(
                        "31-60",
                        width="small"
                    ),
                    "61-90": st.column_config.TextColumn(
                        "61-90",
                        width="small"
                    ),
                    "Total Open": st.column_config.TextColumn(
                        "Total Open",
                        width="small"
                    ),
                    "Risk": st.column_config.TextColumn(
                        "Risk",
                        width="small"
                    ),
                    "Comments": st.column_config.TextColumn(
                        "Comments",
                        width="large"
                    )
                }
            )

            selected_risk_rows = get_selected_dataframe_rows(risk_selection)

            if st.session_state.pop("skip_risk_dialog_once", False):
                selected_risk_rows = []

            if selected_risk_rows:
                selected_row_index = selected_risk_rows[0]
                edit_risk_comment_dialog(
                    risk_editor_df.iloc[selected_row_index].to_dict()
                )

        else:
            st.info("No clients with open invoices between 31 and 90 days past due.")

        # ==================================================
        # CLIENTS WITH INVOICES OVER 90 DAYS
        # ==================================================
        notion_anchor("aging-over-90")
        st.markdown("#### ❗ Clients with Invoices Over 90 Days")
        st.caption("Review clients with open invoices more than 90 days past due.")

        over_90_df = risk_base_df[
            risk_base_df["Days Past Due"] > 90
        ].copy()

        over_90_total = over_90_df["Amount Fixed (USD)"].sum()

        over_90_vs_past_due_pct = (
            over_90_total / total_past_due * 100
        ) if total_past_due > 0 else 0

        over90_card_col1, over90_card_col2, over90_card_col3 = st.columns([1.35, 1.05, 1.6])

        with over90_card_col1:
            small_card(
                "Total Amount Invoices Over 90 Days",
                f"${over_90_total:,.0f}",
                "#DC2626"
            )

        with over90_card_col2:
            small_card(
                "% Of Past Due",
                f"{over_90_vs_past_due_pct:.2f}%",
                "#D97706"
            )

        st.markdown("<br>", unsafe_allow_html=True)

        if len(over_90_df) > 0:

            def classify_over_90_bucket(days):
                if 91 <= days <= 120:
                    return "91-120"
                if 121 <= days <= 360:
                    return "121-360"
                return ">360"

            over_90_df["Bucket"] = over_90_df["Days Past Due"].apply(
                classify_over_90_bucket
            )

            over_90_bucket_order = [
                "91-120",
                "121-360",
                ">360"
            ]

            bad_clients = over_90_df.pivot_table(
                index=["Customer name", "HS ID", "HS Name"],
                columns="Bucket",
                values="Amount Fixed (USD)",
                aggfunc="sum",
                fill_value=0
            ).reset_index()

            for bucket in over_90_bucket_order:
                if bucket not in bad_clients.columns:
                    bad_clients[bucket] = 0

            bad_clients["Total Open"] = bad_clients[over_90_bucket_order].sum(axis=1)

            bad_clients = bad_clients.sort_values(
                "Total Open",
                ascending=False
            )

            bad_clients["Collection Status"] = ""
            bad_clients["Comment Keys"] = bad_clients.apply(
                lambda row: comment_alias_keys(
                    row,
                    primary_key=row.get("Customer name", "")
                ),
                axis=1
            )

            bad_clients = add_comments_from_sheet(
                bad_clients,
                "Clients Over 90",
                "Customer name"
            )
            bad_clients = add_latest_comments_by_alias(
                bad_clients,
                "Clients Over 90"
            )

            for col in over_90_bucket_order + ["Total Open"]:
                bad_clients[col] = bad_clients[col].map(lambda x: f"${x:,.0f}")

            bad_clients["HS ID"] = bad_clients["HS ID"].map(display_hs_value)
            bad_clients["HS Name"] = bad_clients["HS Name"].map(display_hs_value)

            over90_table_height = 88 + (len(bad_clients) * 46)
            over90_table_height = min(max(over90_table_height, 170), 420)

            over90_editor_df = bad_clients.copy().reset_index(drop=True)
            over90_display_df = over90_editor_df.drop(
                columns=["Comment Keys"],
                errors="ignore"
            )

            with st.form("clients_over_90_comments_form"):
                edited_bad = st.data_editor(
                    over90_display_df,
                    key="clients_over_90_editor_compact",
                    width="stretch",
                    height=380,
                    num_rows="fixed",
                    hide_index=True,
                    column_order=[
                        "HS ID",
                        "HS Name",
                        "91-120",
                        "121-360",
                        ">360",
                        "Total Open",
                        "Collection Status",
                        "Comments"
                    ],
                    column_config={
                        "HS ID": st.column_config.TextColumn("HS ID", width="small"),
                        "HS Name": st.column_config.TextColumn("HS Name", width="medium"),
                        "91-120": st.column_config.TextColumn("91-120", width="small"),
                        "121-360": st.column_config.TextColumn("121-360", width="small"),
                        ">360": st.column_config.TextColumn(">360", width="small"),
                        "Total Open": st.column_config.TextColumn("Total Open", width="small"),
                        "Collection Status": st.column_config.SelectboxColumn(
                            "Collection Status",
                            options=[
                                "",
                                "🔴 Uncollectable",
                                "🟢 Possibility of Collection"
                            ],
                            width="medium",
                            required=False
                        ),
                        "Comments": st.column_config.TextColumn("Comments", width="large")
                    },
                    disabled=[
                        "HS ID",
                        "HS Name",
                        "91-120",
                        "121-360",
                        ">360",
                        "Total Open"
                    ]
                )

                save_over_90_comments = st.form_submit_button("Save Comments")

            if save_over_90_comments:
                edited_bad_to_save = edited_bad.reset_index(drop=True)
                over90_rows_to_save = []

                for row_index, row in edited_bad_to_save.iterrows():
                    source_row = over90_editor_df.iloc[row_index]
                    alias_keys = source_row.get("Comment Keys", [])

                    for alias_key in alias_keys:
                        over90_rows_to_save.append({
                            "record_key": alias_key,
                            "client_name": row["HS Name"],
                            "comment": row["Comments"],
                            "collection_status": row["Collection Status"]
                        })

                save_sheet_comment_aliases(
                    "Clients Over 90",
                    over90_rows_to_save
                )

        else:
            st.info("No clients with invoices over 90 days.")

        # ==================================================
        # COLLECTIONS PERFORMANCE BY DUE DATE
        # ==================================================
        due_df = df[df["Due Date"].notna()].copy()

        if selected_year != "All":
            due_df = due_df[
                due_df["Due Date"].dt.year == selected_year
            ]

        due_df["MonthSort"] = due_df["Due Date"].dt.to_period("M")
        due_df["MonthLabel"] = due_df["Due Date"].dt.strftime("%Y-%b")

        chart_df = due_df.groupby(
            ["MonthSort", "MonthLabel", "Status"],
            as_index=False
        )["Amount Fixed (USD)"].sum()

        chart_df["Status"] = pd.Categorical(
            chart_df["Status"],
            categories=["open", "paid"],
            ordered=True
        )

        chart_df = chart_df.sort_values(["MonthSort", "Status"])

        month_order = chart_df["MonthLabel"].drop_duplicates().tolist()
        month_order_recent = (
            chart_df[["MonthSort", "MonthLabel"]]
            .drop_duplicates()
            .sort_values("MonthSort", ascending=False)["MonthLabel"]
            .tolist()
        )

        notion_anchor("aging-collections-due-date")
        st.markdown("## 📊 Collections Performance by Due Date")

        if len(chart_df) > 0:

            fig = px.bar(
                chart_df,
                x="MonthLabel",
                y="Amount Fixed (USD)",
                color="Status",
                barmode="stack",
                category_orders={
                    "MonthLabel": month_order,
                    "Status": ["open", "paid"]
                },
                color_discrete_map={
                    "open": "#B91C1C",
                    "paid": "#15803D"
                }
            )

            fig.update_layout(
                height=480,
                bargap=0.22,
                plot_bgcolor="#FAFAFA",
                paper_bgcolor="#FFFFFF",
                font=dict(size=15, family="Arial"),
                legend_title="Status",
                xaxis_title="Months",
                yaxis_title="Amount USD",
                margin=dict(l=20, r=20, t=20, b=20),
                yaxis=dict(
                    tickprefix="$",
                    separatethousands=True,
                    gridcolor="rgba(120,120,120,0.10)",
                    zeroline=False
                ),
                xaxis=dict(showgrid=False),
                hoverlabel=dict(
                    bgcolor="#0F172A",
                    font_size=11,
                    font_family="Arial Black",
                    font_color="#FFFFFF",
                    bordercolor="#334155",
                    namelength=-1
                )
            )

            fig.update_traces(
                hovertemplate="<b>%{x}</b><br>$%{y:,.0f}<extra></extra>",
                marker_line_width=1.3,
                marker_line_color="rgba(255,255,255,0.20)",
                opacity=0.96
            )

            st.plotly_chart(
                fig,
                width="stretch",
                key="collections_due_date_chart"
            )

            pivot = chart_df.pivot_table(
                index="Status",
                columns="MonthLabel",
                values="Amount Fixed (USD)",
                aggfunc="sum",
                fill_value=0
            )

            pivot = pivot.reindex(
                index=["open", "paid"],
                columns=month_order,
                fill_value=0
            ).fillna(0)

            month_totals = pivot.sum(axis=0)

            percent = pivot.copy()

            for col in percent.columns:
                if month_totals[col] > 0:
                    percent[col] = percent[col] / month_totals[col] * 100
                else:
                    percent[col] = 0

            monthly_collection_table = pd.DataFrame(
                columns=month_order
            )

            monthly_collection_table.loc["🔴 Open %"] = percent.loc[
                "open"
            ].map(lambda x: f"{x:.0f}%")

            monthly_collection_table.loc["🟢 Paid %"] = percent.loc[
                "paid"
            ].map(lambda x: f"{x:.0f}%")

            monthly_collection_table.loc["🔴 Open Amount"] = pivot.loc[
                "open"
            ].map(lambda x: f"${x:,.0f}")

            monthly_collection_table.loc["🟢 Paid Amount"] = pivot.loc[
                "paid"
            ].map(lambda x: f"${x:,.0f}")

            monthly_collection_table = monthly_collection_table.reset_index()
            monthly_collection_table = monthly_collection_table.rename(
                columns={"index": "Metric"}
            )

            notion_anchor("aging-monthly-collection")
            st.markdown("### Monthly Collection %")

            monthly_header_html = "".join(
                f"<th>{html.escape(str(col))}</th>"
                for col in monthly_collection_table.columns
            )

            monthly_rows_html = ""

            for _, row in monthly_collection_table.iterrows():
                monthly_rows_html += "<tr>"

                for col in monthly_collection_table.columns:
                    monthly_rows_html += (
                        f"<td>{html.escape(str(row[col]))}</td>"
                    )

                monthly_rows_html += "</tr>"

            st.markdown(
                f"""
                <style>
                .monthly-collection-wrap {{
                    width:100%;
                    max-height:210px;
                    overflow:auto;
                    border:1px solid #E5E7EB;
                    border-radius:10px;
                    background:#FFFFFF;
                }}
                table.monthly-collection-table {{
                    border-collapse:separate;
                    border-spacing:0;
                    min-width:980px;
                    width:max-content;
                    font-family:Arial, sans-serif;
                    font-size:13px;
                    color:#334155;
                }}
                table.monthly-collection-table th,
                table.monthly-collection-table td {{
                    padding:11px 14px;
                    border-right:1px solid #E5E7EB;
                    border-bottom:1px solid #E5E7EB;
                    white-space:nowrap;
                    background:#FFFFFF;
                }}
                table.monthly-collection-table thead th {{
                    position:sticky;
                    top:0;
                    z-index:3;
                    background:#F8FAFC;
                    color:#64748B;
                    font-weight:600;
                }}
                table.monthly-collection-table th:first-child,
                table.monthly-collection-table td:first-child {{
                    position:sticky;
                    left:0;
                    z-index:2;
                    min-width:145px;
                    background:#FFFFFF;
                    box-shadow:2px 0 0 #E5E7EB;
                    font-weight:650;
                }}
                table.monthly-collection-table thead th:first-child {{
                    z-index:4;
                    background:#F8FAFC;
                }}
                </style>
                <div class="monthly-collection-wrap">
                    <table class="monthly-collection-table">
                        <thead><tr>{monthly_header_html}</tr></thead>
                        <tbody>{monthly_rows_html}</tbody>
                    </table>
                </div>
                """,
                unsafe_allow_html=True
            )

            notion_anchor("aging-analyze-month")
            st.markdown("<div style='height:22px;'></div>", unsafe_allow_html=True)
            st.markdown("### 🔎 Analyze Month")

            col1, col2 = st.columns(2)

            with col1:
                selected_month = st.selectbox(
                    "Select Month",
                    month_order_recent,
                    index=0,
                    key="due_month_tab1"
                )

            with col2:
                selected_status = st.selectbox(
                    "Select Status",
                    ["open", "paid"],
                    key="due_status_tab1"
                )

            detail = due_df[
                (due_df["MonthLabel"] == selected_month) &
                (due_df["Status"] == selected_status)
            ].copy()

            total_selected = detail["Amount Fixed (USD)"].sum()

            if len(detail) > 0:

                top10 = detail.groupby(
                    ["HS ID", "HS Name", "Due Date"],
                    as_index=False
                )["Amount Fixed (USD)"].sum()

                top10 = top10.sort_values(
                    "Amount Fixed (USD)",
                    ascending=False
                ).head(10)

                top10["%"] = (
                    top10["Amount Fixed (USD)"] /
                    total_selected * 100
                ).fillna(0)

                top10["Due Date"] = pd.to_datetime(
                    top10["Due Date"]
                ).dt.strftime("%Y-%m-%d")

                top10["Amount Fixed (USD)"] = top10[
                    "Amount Fixed (USD)"
                ].map(lambda x: f"${x:,.0f}")

                top10["%"] = top10["%"].map(lambda x: f"{x:.0f}%")
                top10["HS ID"] = top10["HS ID"].map(display_hs_value)
                top10["HS Name"] = top10["HS Name"].map(display_hs_value)

                st.markdown(
                    f"### Top 10 {selected_status.upper()} Clients - {selected_month}"
                )

                st.dataframe(
                    top10.rename(columns={
                        "Amount Fixed (USD)": "Amount USD"
                    }),
                    width="stretch",
                    height=420,
                    hide_index=True
                )

            else:
                st.info("No data available for this selection.")

        else:
            st.info("No due date data available for this selection.")





# ==================================================
# TAB 4 - INVOICE VOLUME PERFORMANCE
# ==================================================
if section_is_visible("invoice-volume"):
    tab_context = tab2 if active_section == "all" else st.container()

    with tab_context:

        import os
        import plotly.graph_objects as go
        from datetime import datetime
        from urllib.parse import quote

        # ==================================================
        # PROFESSIONAL DESIGN HELPERS
        # ==================================================
        PRO_COLORS = {
            "blue": "#2563EB",
            "blue_dark": "#1E3A8A",
            "green": "#15803D",
            "red": "#B91C1C",
            "amber": "#D97706",
            "slate": "#475569",
            "border": "#E5E7EB",
            "grid": "rgba(148,163,184,0.20)",
            "text": "#0F172A",
            "body": "#334155",
            "muted": "#64748B"
        }

        CHART_LABEL_FONT = dict(
            family="Arial",
            size=11,
            color="#334155"
        )

        GOOGLE_STRIPE_SHEET_ID = "114oEoIZLBWxnXbQlm5qcnmGrmT0WBnAJzpQ6Kvb3XIY"
        CREDIT_NOTES_WORKSHEET_NAME = "stripe_credit_notes"
        REFUNDS_WORKSHEET_NAME = "stripe_refunds"

        st.markdown("""
        <style>
        .pro-title {
            font-size:30px;
            font-weight:850;
            color:#0F172A;
            letter-spacing:0;
            margin-bottom:8px;
        }
        .pro-subtitle {
            color:#64748B;
            font-size:13px;
            font-weight:500;
            margin-bottom:18px;
        }
        .pro-card {
            background:#FFFFFF;
            padding:14px 16px;
            border-radius:10px;
            border:1px solid #E5E7EB;
            box-shadow:0 3px 12px rgba(15,23,42,.06);
            min-height:88px;
            display:flex;
            flex-direction:column;
            justify-content:center;
        }
        .pro-card-blue { border-left:5px solid #2563EB; }
        .pro-card-green { border-left:5px solid #16A34A; }
        .pro-card-red { border-left:5px solid #DC2626; }
        .pro-card-amber { border-left:5px solid #D97706; }
        .pro-card-slate { border-left:5px solid #475569; }
        .pro-card-title {
            font-size:12px;
            color:#64748B;
            font-weight:600;
            margin-bottom:8px;
        }
        .pro-card-value {
            font-size:24px;
            color:#0F172A;
            font-weight:800;
            line-height:1;
            white-space:nowrap;
        }
        .pro-card-sub {
            color:#64748B;
            font-size:12px;
            font-weight:500;
            margin-top:8px;
        }

        .pro-static-table-wrap {
            width:100%;
            overflow:auto;
            border:1px solid #E5E7EB;
            border-radius:10px;
            background:#FFFFFF;
        }
        table.pro-html-table {
            width:100%;
            border-collapse:collapse;
            font-family:Arial, sans-serif;
            font-size:13px;
            color:#334155;
            background:#FFFFFF;
            table-layout:auto;
        }
        table.pro-html-table thead th {
            background:#F8FAFC;
            color:#64748B;
            font-weight:500;
            text-align:left;
            padding:11px 12px;
            border-bottom:1px solid #E5E7EB;
            border-right:1px solid #E5E7EB;
            white-space:nowrap;
        }
        table.pro-html-table tbody td {
            color:#334155;
            font-weight:400;
            padding:10px 12px;
            border-bottom:1px solid #E5E7EB;
            border-right:1px solid #E5E7EB;
            vertical-align:middle;
            white-space:normal;
        }
        table.pro-html-table tbody tr:hover td {
            background:#F8FAFC;
        }

        div[data-testid="stDataFrame"],
        div[data-testid="stDataEditor"] {
            font-family:Arial, sans-serif !important;
            -webkit-font-smoothing:antialiased !important;
            -moz-osx-font-smoothing:grayscale !important;
            text-rendering:geometricPrecision !important;
        }
        div[data-testid="stDataFrame"] div,
        div[data-testid="stDataEditor"] div {
            color:#334155 !important;
            font-weight:400 !important;
            text-shadow:none !important;
            -webkit-font-smoothing:antialiased !important;
            -moz-osx-font-smoothing:grayscale !important;
        }
        div[data-testid="stDataFrame"] [role="columnheader"] div,
        div[data-testid="stDataEditor"] [role="columnheader"] div {
            color:#64748B !important;
            font-weight:500 !important;
            text-shadow:none !important;
        }
        </style>
        """, unsafe_allow_html=True)

        def pro_card(title, value, color_class, subtitle=""):
            subtitle_html = f"<div class='pro-card-sub'>{subtitle}</div>" if subtitle else ""
            st.markdown(f"""
            <div class="pro-card {color_class}">
                <div class="pro-card-title">{title}</div>
                <div class="pro-card-value">{value}</div>
                {subtitle_html}
            </div>
            """, unsafe_allow_html=True)

        def render_pro_table(dataframe, height=None, index=False):
            table_html = dataframe.to_html(
                index=index,
                classes="pro-html-table",
                border=0,
                escape=True
            )

            height_style = f"max-height:{height}px;" if height else ""

            st.markdown(
                f"""
                <div class="pro-static-table-wrap" style="{height_style}">
                    {table_html}
                </div>
                """,
                unsafe_allow_html=True
            )

        def format_money(value):
            try:
                value = float(value)
            except:
                value = 0
            return f"${value:,.0f}"

        def format_number(value):
            try:
                value = int(value)
            except:
                value = 0
            return f"{value:,}"

        def label_number(value):
            try:
                return f"{int(value):,}"
            except:
                return "0"

        def pro_chart_layout(fig, height=360, showlegend=True, tickangle=-35):
            fig.update_layout(
                height=height,
                plot_bgcolor="#FFFFFF",
                paper_bgcolor="#FFFFFF",
                font=dict(
                    family="Arial",
                    size=12,
                    color=PRO_COLORS["text"]
                ),
                margin=dict(l=24, r=24, t=64, b=70),
                hoverlabel=dict(
                    bgcolor="#0F172A",
                    font_color="white",
                    font_size=12
                ),
                legend=dict(
                    orientation="h",
                    yanchor="bottom",
                    y=1.04,
                    xanchor="right",
                    x=1,
                    font=dict(size=12)
                ),
                uniformtext=dict(
                    minsize=11,
                    mode="show"
                ),
                showlegend=showlegend
            )

            fig.update_xaxes(
                showgrid=False,
                tickangle=tickangle,
                color=PRO_COLORS["muted"],
                tickfont=dict(size=11)
            )

            fig.update_yaxes(
                gridcolor=PRO_COLORS["grid"],
                zeroline=False,
                color=PRO_COLORS["muted"],
                tickfont=dict(size=11)
            )

            return fig

        # ==================================================
        # GOOGLE SHEETS STRIPE DATA HELPERS
        # ==================================================
        @st.cache_data(ttl=300, show_spinner=False)
        def load_google_sheet_dataframe(worksheet_name):
            try:
                return load_google_sheet(
                    GOOGLE_STRIPE_SHEET_ID,
                    sheet_name=worksheet_name
                )
            except Exception as e:
                st.session_state.google_stripe_sheet_error = str(e)
                return pd.DataFrame()

        def clean_money_series(series):
            return pd.to_numeric(
                series.astype(str)
                .str.replace("$", "", regex=False)
                .str.replace(",", "", regex=False)
                .str.strip(),
                errors="coerce"
            ).fillna(0)

        def prepare_credit_notes_from_google_sheet():
            credit_notes_df = load_google_sheet_dataframe(CREDIT_NOTES_WORKSHEET_NAME)

            expected_columns = [
                "credit_note_id",
                "credit_note_number",
                "created",
                "customer_name",
                "invoice_number",
                "amount",
                "currency",
                "status",
                "reason",
                "customer_id"
            ]

            for col in expected_columns:
                if col not in credit_notes_df.columns:
                    credit_notes_df[col] = ""

            if len(credit_notes_df) == 0:
                return credit_notes_df

            credit_notes_df["created"] = pd.to_datetime(
                credit_notes_df["created"],
                errors="coerce"
            )

            credit_notes_df["amount"] = clean_money_series(
                credit_notes_df["amount"]
            )

            credit_notes_df["currency"] = (
                credit_notes_df["currency"]
                .astype(str)
                .str.upper()
                .str.strip()
            )

            credit_notes_df = credit_notes_df[
                credit_notes_df["created"].notna()
            ].copy()

            if len(credit_notes_df) == 0:
                return credit_notes_df

            credit_notes_df["Year"] = credit_notes_df["created"].dt.year.astype(int)
            credit_notes_df["MonthNum"] = credit_notes_df["created"].dt.month
            credit_notes_df["Month"] = credit_notes_df["created"].dt.strftime("%Y-%b")
            credit_notes_df["MonthSort"] = credit_notes_df["created"].dt.to_period("M")

            return credit_notes_df

        def prepare_refunds_from_google_sheet():
            refunds_df = load_google_sheet_dataframe(REFUNDS_WORKSHEET_NAME)

            expected_columns = [
                "refund_id",
                "created",
                "customer_name",
                "amount",
                "currency",
                "status",
                "reason",
                "charge_id",
                "invoice_number",
                "customer_id"
            ]

            for col in expected_columns:
                if col not in refunds_df.columns:
                    refunds_df[col] = ""

            if len(refunds_df) == 0:
                return refunds_df

            refunds_df["created"] = pd.to_datetime(
                refunds_df["created"],
                errors="coerce"
            )

            refunds_df["amount"] = clean_money_series(
                refunds_df["amount"]
            )

            refunds_df["currency"] = (
                refunds_df["currency"]
                .astype(str)
                .str.upper()
                .str.strip()
            )

            refunds_df["status"] = (
                refunds_df["status"]
                .astype(str)
                .str.lower()
                .str.strip()
            )

            refunds_df = refunds_df[
                refunds_df["created"].notna()
            ].copy()

            refunds_df = refunds_df[
                refunds_df["status"] == "succeeded"
            ].copy()

            if len(refunds_df) == 0:
                return refunds_df

            refunds_df["Year"] = refunds_df["created"].dt.year.astype(int)
            refunds_df["MonthNum"] = refunds_df["created"].dt.month
            refunds_df["Month"] = refunds_df["created"].dt.strftime("%Y-%b")
            refunds_df["MonthSort"] = refunds_df["created"].dt.to_period("M")

            return refunds_df

        # ==================================================
        # MAIN DATASET
        # ==================================================
        notion_anchor("invoice-overview")
        st.markdown("<div class='pro-title'>Invoice Volume Performance</div>", unsafe_allow_html=True)
        st.markdown("<div class='pro-subtitle'>Open and paid invoice activity by creation month.</div>", unsafe_allow_html=True)

        vol_df = df.copy()

        vol_df = vol_df[
            vol_df["Creation Date"].notna()
        ].copy()

        vol_df["Status"] = (
            vol_df["Status"]
            .astype(str)
            .str.lower()
            .str.strip()
        )

        vol_df = vol_df[
            vol_df["Status"].isin(["open", "paid"])
        ].copy()

        vol_df = vol_df[
            vol_df["Amount Fixed (USD)"] > 0
        ].copy()

        if selected_year != "All":
            vol_df = vol_df[
                vol_df["Creation Date"].dt.year == selected_year
            ].copy()

        vol_df["MonthSort"] = vol_df["Creation Date"].dt.to_period("M")
        vol_df["MonthLabel"] = vol_df["Creation Date"].dt.strftime("%Y-%b")

        vol_df["Due Date"] = pd.to_datetime(
            vol_df["Due Date"],
            errors="coerce"
        )

        vol_df["DueMonth"] = vol_df["Due Date"].dt.strftime("%Y-%b")
        vol_df["DueSort"] = vol_df["Due Date"].dt.to_period("M")

        total_inv = vol_df.groupby(
            ["MonthSort", "MonthLabel"],
            as_index=False
        )["ID"].count().rename(columns={"ID": "Total"})

        paid_inv = vol_df[
            vol_df["Status"] == "paid"
        ].groupby(
            ["MonthSort", "MonthLabel"],
            as_index=False
        )["ID"].count().rename(columns={"ID": "Paid"})

        open_inv = vol_df[
            vol_df["Status"] == "open"
        ].groupby(
            ["MonthSort", "MonthLabel"],
            as_index=False
        )["ID"].count().rename(columns={"ID": "Open"})

        chart4 = total_inv.merge(
            paid_inv,
            on=["MonthSort", "MonthLabel"],
            how="left"
        ).merge(
            open_inv,
            on=["MonthSort", "MonthLabel"],
            how="left"
        ).fillna(0)

        chart4["Total"] = chart4["Total"].astype(int)
        chart4["Paid"] = chart4["Paid"].astype(int)
        chart4["Open"] = chart4["Open"].astype(int)

        chart4["Open %"] = (
            chart4["Open"] / chart4["Total"] * 100
        ).round(1)

        chart4 = chart4.sort_values("MonthSort")

        total_kpi = int(chart4["Total"].sum())
        paid_kpi = int(chart4["Paid"].sum())
        open_kpi = int(chart4["Open"].sum())

        paid_pct = (paid_kpi / total_kpi * 100) if total_kpi > 0 else 0
        open_pct = (open_kpi / total_kpi * 100) if total_kpi > 0 else 0

        c1, c2, c3 = st.columns(3)

        with c1:
            pro_card(
                "Total Invoices",
                format_number(total_kpi),
                "pro-card-blue",
                "Only Open + Paid"
            )

        with c2:
            pro_card(
                "Paid",
                format_number(paid_kpi),
                "pro-card-green",
                f"{paid_pct:.1f}% of total"
            )

        with c3:
            pro_card(
                "Open",
                format_number(open_kpi),
                "pro-card-red",
                f"{open_pct:.1f}% of total"
            )

        st.markdown("<br>", unsafe_allow_html=True)

        # ==================================================
        # INVOICE VOLUME CHART
        # ==================================================
        fig4 = go.Figure()

        fig4.add_trace(
            go.Bar(
                x=chart4["MonthLabel"],
                y=chart4["Paid"],
                name="Paid",
                marker_color="rgba(21,128,61,0.88)",
                hovertemplate="<b>%{x}</b><br>Paid: %{y}<extra></extra>"
            )
        )

        fig4.add_trace(
            go.Bar(
                x=chart4["MonthLabel"],
                y=chart4["Open"],
                name="Open",
                marker_color="rgba(185,28,28,0.84)",
                hovertemplate="<b>%{x}</b><br>Open: %{y}<extra></extra>"
            )
        )

        fig4.update_layout(
            barmode="stack",
            xaxis_title="Month",
            yaxis_title="Invoices Created"
        )

        fig4 = pro_chart_layout(fig4, height=390)

        st.plotly_chart(
            fig4,
            width="stretch",
            key="invoice_volume_chart"
        )

        # ==================================================
        # MONTHLY DETAIL
        # ==================================================
        open_only = vol_df[
            vol_df["Status"] == "open"
        ].copy()

        due_map = {}

        for month in chart4["MonthLabel"]:

            temp = open_only[
                open_only["MonthLabel"] == month
            ].copy()

            if len(temp) == 0:
                due_map[month] = "-"
                continue

            parts = []

            with_due = temp[
                temp["Due Date"].notna()
            ].copy()

            if len(with_due) > 0:

                due_counts = with_due.groupby(
                    ["DueSort", "DueMonth"]
                )["ID"].count().reset_index()

                due_counts = due_counts.sort_values("DueSort")

                for _, row in due_counts.iterrows():
                    parts.append(
                        f"{row['DueMonth']}: {int(row['ID'])}"
                    )

            no_due = temp["Due Date"].isna().sum()

            if no_due > 0:
                parts.append(f"No Due Date: {int(no_due)}")

            due_map[month] = " | ".join(parts)

        notion_anchor("invoice-monthly-detail")
        st.markdown("### Monthly Detail")

        summary = chart4.copy()

        summary["Open Due Months"] = summary[
            "MonthLabel"
        ].map(due_map)

        summary = summary.sort_values(
            "MonthSort",
            ascending=False
        )

        summary = summary[
            [
                "MonthLabel",
                "Total",
                "Open",
                "Paid",
                "Open Due Months",
                "Open %"
            ]
        ]

        summary.columns = [
            "Month",
            "Invoices Created",
            "Open",
            "Paid",
            "Open Due Months",
            "Open %"
        ]

        summary["Open %"] = summary["Open %"].map(
            lambda x: f"{x:.0f}%"
        )

        render_pro_table(summary, height=420)

        # ==================================================
        # MONTHLY BILLING CHURN ANALYSIS
        # ==================================================
        notion_anchor("invoice-churn")
        st.markdown("#### Monthly Billing Churn Cases")
        st.caption(
            "Clients billed in the previous month but not billed again in the selected month. "
            "Clients are matched by HS ID first, then Stripe Customer ID. "
            "Only invoices with Due Date are included."
        )

        churn_df = df[
            (df["Creation Date"].notna()) &
            (df["Due Date"].notna())
        ].copy()

        for identity_col in ["ID", "Customer name", "HS ID", "HS Name"]:
            if identity_col not in churn_df.columns:
                churn_df[identity_col] = ""

            churn_df[identity_col] = churn_df[identity_col].map(
                clean_identity_value
            )

        def build_churn_client_key(row):
            hs_id = clean_identity_value(row.get("HS ID", ""))
            customer_id = clean_identity_value(row.get("ID", ""))

            if hs_id:
                return f"hs:{hs_id}"

            if customer_id:
                return f"stripe:{customer_id}"

            return ""

        churn_df["Client Key"] = churn_df.apply(
            build_churn_client_key,
            axis=1
        )

        churn_df["Client"] = churn_df.apply(
            lambda row: (
                clean_identity_value(row.get("HS Name", ""))
                or clean_identity_value(row.get("Customer name", ""))
                or clean_identity_value(row.get("ID", ""))
            ),
            axis=1
        )

        churn_df = churn_df[churn_df["Client Key"] != ""].copy()

        churn_df["MonthSort"] = churn_df["Creation Date"].dt.to_period("M")
        churn_df["MonthLabel"] = churn_df["Creation Date"].dt.strftime("%Y-%b")
        churn_df["Year"] = churn_df["Creation Date"].dt.year.astype(int)

        def first_clean_value(series):
            for item in series:
                value = clean_identity_value(item)

                if value:
                    return value

            return ""

        monthly_client_amount = churn_df.groupby(
            ["MonthSort", "MonthLabel", "Year", "Client Key"],
            as_index=False
        ).agg(
            **{
                "Amount Fixed (USD)": ("Amount Fixed (USD)", "sum"),
                "Client": ("Client", first_clean_value),
                "Customer ID": ("ID", first_clean_value),
                "HS ID": ("HS ID", first_clean_value),
                "HS Name": ("HS Name", first_clean_value)
            }
        )

        all_months = (
            monthly_client_amount[["MonthSort", "MonthLabel", "Year"]]
            .drop_duplicates()
            .sort_values("MonthSort")
            .reset_index(drop=True)
        )

        churn_rows = []
        churn_detail_rows = []

        for idx in range(1, len(all_months)):

            current_month = all_months.loc[idx, "MonthSort"]
            current_label = all_months.loc[idx, "MonthLabel"]
            current_year = all_months.loc[idx, "Year"]

            previous_month = all_months.loc[idx - 1, "MonthSort"]

            previous_data = monthly_client_amount[
                monthly_client_amount["MonthSort"] == previous_month
            ].copy()

            current_data = monthly_client_amount[
                monthly_client_amount["MonthSort"] == current_month
            ].copy()

            previous_clients = set(previous_data["Client Key"])
            current_clients = set(current_data["Client Key"])

            churn_clients = sorted(previous_clients - current_clients)

            current_total_billing = current_data["Amount Fixed (USD)"].sum()

            valid_churn_clients = []

            for client_key in churn_clients:

                client_history_before_current = monthly_client_amount[
                    (monthly_client_amount["Client Key"] == client_key) &
                    (monthly_client_amount["MonthSort"] < current_month)
                ].copy()

                last_invoice_month = client_history_before_current["MonthSort"].max()
                last_invoice_label = ""

                if pd.notna(last_invoice_month):
                    last_invoice_label = (
                        client_history_before_current[
                            client_history_before_current["MonthSort"] == last_invoice_month
                        ]["MonthLabel"]
                        .iloc[0]
                    )

                last_3_months = [
                    current_month - 1,
                    current_month - 2,
                    current_month - 3
                ]

                last_3_data = monthly_client_amount[
                    (monthly_client_amount["Client Key"] == client_key) &
                    (monthly_client_amount["MonthSort"].isin(last_3_months))
                ].copy()

                avg_last_3_months = (
                    last_3_data["Amount Fixed (USD)"].sum() / 3
                )

                if avg_last_3_months < 100:
                    continue

                valid_churn_clients.append(client_key)

                percent_of_period_billing = (
                    avg_last_3_months / current_total_billing * 100
                ) if current_total_billing > 0 else 0

                client_identity = client_history_before_current.tail(1).iloc[0]

                churn_detail_rows.append({
                    "MonthSort": current_month,
                    "Month": current_label,
                    "Year": current_year,
                    "Client Key": client_key,
                    "Client": client_identity.get("Client", ""),
                    "Customer ID": client_identity.get("Customer ID", ""),
                    "HS ID": client_identity.get("HS ID", ""),
                    "HS Name": client_identity.get(
                        "HS Name",
                        client_identity.get("Client", "")
                    ),
                    "Last Invoice Sent": last_invoice_label,
                    "Avg Last 3 Months Billed": avg_last_3_months,
                    "% of Period Billing": percent_of_period_billing
                })

            churn_rows.append({
                "MonthSort": current_month,
                "Month": current_label,
                "Year": current_year,
                "Churn Cases": len(valid_churn_clients)
            })

        churn_chart_df = pd.DataFrame(churn_rows)
        churn_detail_df = pd.DataFrame(churn_detail_rows)

        if len(churn_chart_df) > 0:

            churn_year_options = ["All"] + sorted(
                churn_chart_df["Year"].dropna().unique().astype(int).tolist(),
                reverse=True
            )

            filter_col1, filter_col2 = st.columns([1, 1])

            with filter_col1:
                selected_churn_year = st.selectbox(
                    "Select Churn Year",
                    churn_year_options,
                    index=0,
                    key="selected_churn_year"
                )

            if selected_churn_year == "All":
                filtered_churn_chart_df = churn_chart_df.copy()
                filtered_churn_detail_df = churn_detail_df.copy()
            else:
                filtered_churn_chart_df = churn_chart_df[
                    churn_chart_df["Year"] == selected_churn_year
                ].copy()

                filtered_churn_detail_df = churn_detail_df[
                    churn_detail_df["Year"] == selected_churn_year
                ].copy()

            filtered_churn_chart_df = filtered_churn_chart_df.sort_values("MonthSort")
            filtered_churn_detail_df = filtered_churn_detail_df.sort_values("MonthSort")

            if len(filtered_churn_chart_df) > 0:

                churn_month_options = (
                    filtered_churn_chart_df[["MonthSort", "Month"]]
                    .drop_duplicates()
                    .sort_values("MonthSort", ascending=False)["Month"]
                    .tolist()
                )

                with filter_col2:
                    selected_churn_month = st.selectbox(
                        "Select Churn Month",
                        churn_month_options,
                        index=0,
                        key="selected_churn_month"
                    )

                selected_churn_detail = filtered_churn_detail_df[
                    filtered_churn_detail_df["Month"] == selected_churn_month
                ].copy()

                selected_churn_cases = int(
                    filtered_churn_chart_df[
                        filtered_churn_chart_df["Month"] == selected_churn_month
                    ]["Churn Cases"].sum()
                )

                selected_avg_billing = (
                    selected_churn_detail["Avg Last 3 Months Billed"].sum()
                    if len(selected_churn_detail) > 0
                    else 0
                )

                card_col1, card_col2 = st.columns(2)

                with card_col1:
                    pro_card(
                        "Churn Cases > $100",
                        format_number(selected_churn_cases),
                        "pro-card-red"
                    )

                with card_col2:
                    pro_card(
                        "Selected Month Avg Billing",
                        format_money(selected_avg_billing),
                        "pro-card-blue"
                    )

                st.markdown("<br>", unsafe_allow_html=True)

                fig_churn = go.Figure()

                fig_churn.add_trace(
                    go.Bar(
                        x=filtered_churn_chart_df["Month"],
                        y=filtered_churn_chart_df["Churn Cases"],
                        name="Churn Cases",
                        marker_color="rgba(220,38,38,0.34)",
                        width=0.38,
                        text=filtered_churn_chart_df["Churn Cases"].map(label_number),
                        textposition="outside",
                        textfont=CHART_LABEL_FONT,
                        cliponaxis=False,
                        hovertemplate="<b>%{x}</b><br>Churn Cases: %{y}<extra></extra>"
                    )
                )

                tick_values = filtered_churn_chart_df["Month"].tolist()

                if selected_churn_year == "All" and len(tick_values) > 18:
                    tick_values = tick_values[::3]

                fig_churn.update_layout(
                    xaxis_title="Month",
                    yaxis_title="Churn Cases",
                    xaxis=dict(
                        tickmode="array",
                        tickvals=tick_values
                    )
                )

                fig_churn = pro_chart_layout(fig_churn, height=340)

                st.plotly_chart(
                    fig_churn,
                    width="stretch",
                    key="monthly_billing_churn_chart"
                )

                if len(selected_churn_detail) > 0:

                    selected_churn_detail = selected_churn_detail.sort_values(
                        "Avg Last 3 Months Billed",
                        ascending=False
                    )

                    selected_churn_detail["Comment Key"] = (
                        selected_churn_detail["Month"].astype(str) +
                        " - " +
                        selected_churn_detail["Client Key"].astype(str)
                    )

                    selected_churn_detail = add_comments_from_sheet(
                        selected_churn_detail,
                        "Churn Detail",
                        "Comment Key"
                    )

                    selected_churn_detail["HS ID"] = selected_churn_detail[
                        "HS ID"
                    ].map(display_hs_value)

                    selected_churn_detail["HS Name"] = selected_churn_detail[
                        "HS Name"
                    ].map(display_hs_value)

                    selected_churn_detail["Avg Last 3 Months Billed"] = selected_churn_detail[
                        "Avg Last 3 Months Billed"
                    ].map(lambda x: f"${x:,.0f}")

                    selected_churn_detail["% of Period Billing"] = selected_churn_detail[
                        "% of Period Billing"
                    ].map(lambda x: f"{x:.0f}%")

                    selected_churn_detail_editor = selected_churn_detail[
                        [
                            "Comment Key",
                            "Client",
                            "HS ID",
                            "HS Name",
                            "Last Invoice Sent",
                            "Avg Last 3 Months Billed",
                            "% of Period Billing",
                            "Comments"
                        ]
                    ].copy().reset_index(drop=True)

                    st.markdown(
                        f"#### Churn Detail - {selected_churn_month}"
                    )

                    with st.form("churn_detail_comments_form"):
                        edited_churn_detail = st.data_editor(
                            selected_churn_detail_editor,
                            width="stretch",
                            height=360,
                            hide_index=True,
                            key="churn_detail_editor",
                            column_order=[
                                "HS ID",
                                "HS Name",
                                "Last Invoice Sent",
                                "Avg Last 3 Months Billed",
                                "% of Period Billing",
                                "Comments"
                            ],
                            column_config={
                                "HS ID": st.column_config.TextColumn(
                                    "HS ID",
                                    width="small"
                                ),
                                "HS Name": st.column_config.TextColumn(
                                    "HS Name",
                                    width="medium"
                                ),
                                "Last Invoice Sent": st.column_config.TextColumn(
                                    "Last Invoice Sent",
                                    width="small"
                                ),
                                "Avg Last 3 Months Billed": st.column_config.TextColumn(
                                    "Avg Last 3 Months Billed",
                                    width="small"
                                ),
                                "% of Period Billing": st.column_config.TextColumn(
                                    "% of Period Billing",
                                    width="small"
                                ),
                                "Comments": st.column_config.TextColumn(
                                    "Comments",
                                    width="large"
                                )
                            },
                            disabled=[
                                "HS ID",
                                "HS Name",
                                "Last Invoice Sent",
                                "Avg Last 3 Months Billed",
                                "% of Period Billing"
                            ]
                        )

                        save_churn_comments = st.form_submit_button("Save Comments")

                    if save_churn_comments:
                        save_sheet_comments_batch(
                            "Churn Detail",
                            [
                                {
                                    "record_key": selected_churn_detail_editor.iloc[row_index]["Comment Key"],
                                    "client_name": row["HS Name"],
                                    "comment": row["Comments"]
                                }
                                for row_index, row in edited_churn_detail.iterrows()
                            ]
                        )

                else:
                    st.info(
                        "No churn clients over $100 found for this month."
                    )

            else:
                st.info(
                    "No churn data available for this year."
                )

        else:
            st.info(
                "Not enough monthly data to calculate churn cases."
            )

        # ==================================================
        # GOOGLE SHEETS CREDIT NOTES
        # ==================================================
        notion_anchor("invoice-credit-notes")
        st.markdown("### Credit Notes Issued")

        credit_notes_df = prepare_credit_notes_from_google_sheet()

        if len(credit_notes_df) > 0:

            if selected_currency != "All":
                credit_notes_df = credit_notes_df[
                    credit_notes_df["currency"] == selected_currency
                ].copy()

            if len(credit_notes_df) > 0:

                credit_note_year_options = ["All"] + sorted(
                    credit_notes_df["Year"].dropna().unique().astype(int).tolist(),
                    reverse=True
                )

                cn_filter_col1, cn_filter_col2 = st.columns(2)

                with cn_filter_col1:
                    selected_credit_note_year = st.selectbox(
                        "Select Credit Note Year",
                        credit_note_year_options,
                        index=0,
                        key="selected_credit_note_year"
                    )

                if selected_credit_note_year == "All":
                    year_credit_notes_df = credit_notes_df.copy()
                else:
                    year_credit_notes_df = credit_notes_df[
                        credit_notes_df["Year"] == selected_credit_note_year
                    ].copy()

                credit_note_month_options = ["All"] + (
                    year_credit_notes_df[["MonthSort", "Month"]]
                    .drop_duplicates()
                    .sort_values("MonthSort", ascending=False)["Month"]
                    .tolist()
                )

                with cn_filter_col2:
                    selected_credit_note_month = st.selectbox(
                        "Select Credit Note Month",
                        credit_note_month_options,
                        index=0,
                        key="selected_credit_note_month"
                    )

                if selected_credit_note_month == "All":
                    filtered_credit_notes_df = year_credit_notes_df.copy()
                else:
                    filtered_credit_notes_df = year_credit_notes_df[
                        year_credit_notes_df["Month"] == selected_credit_note_month
                    ].copy()

                credit_note_chart_source_df = year_credit_notes_df.copy()

                if len(credit_note_chart_source_df) > 0:

                    credit_note_chart_df = credit_note_chart_source_df.groupby(
                        ["MonthSort", "Month"],
                        as_index=False
                    ).agg(
                        Credit_Notes_Issued=("credit_note_id", "count"),
                        Credit_Notes_Amount=("amount", "sum")
                    )

                    credit_note_chart_df = credit_note_chart_df.sort_values("MonthSort")

                    fig_credit_notes = go.Figure()

                    fig_credit_notes.add_trace(
                        go.Bar(
                            x=credit_note_chart_df["Month"],
                            y=credit_note_chart_df["Credit_Notes_Issued"],
                            name="Credit Notes",
                            marker_color="rgba(220,38,38,0.34)",
                            width=0.38,
                            text=credit_note_chart_df["Credit_Notes_Issued"].map(label_number),
                            textposition="outside",
                            textfont=CHART_LABEL_FONT,
                            cliponaxis=False,
                            customdata=credit_note_chart_df["Credit_Notes_Amount"],
                            hovertemplate=(
                                "<b>%{x}</b><br>"
                                "Credit Notes: %{y}<br>"
                                "Amount: $%{customdata:,.0f}"
                                "<extra></extra>"
                            )
                        )
                    )

                    credit_note_tick_values = credit_note_chart_df["Month"].tolist()

                    if selected_credit_note_year == "All" and len(credit_note_tick_values) > 18:
                        credit_note_tick_values = credit_note_tick_values[::3]

                    fig_credit_notes.update_layout(
                        xaxis_title="Month",
                        yaxis_title="Credit Notes Issued",
                        xaxis=dict(
                            tickmode="array",
                            tickvals=credit_note_tick_values
                        )
                    )

                    fig_credit_notes = pro_chart_layout(fig_credit_notes, height=380)

                    st.plotly_chart(
                        fig_credit_notes,
                        width="stretch",
                        key="credit_notes_issued_bar_line_chart"
                    )

                    notion_anchor("invoice-credit-notes-detail")
                    st.markdown("### Credit Notes Detail")

                    if len(filtered_credit_notes_df) > 0:

                        credit_notes_detail = filtered_credit_notes_df[
                            [
                                "created",
                                "customer_id",
                                "customer_name",
                                "credit_note_number",
                                "invoice_number",
                                "amount",
                                "currency"
                            ]
                        ].copy()

                        credit_notes_detail = credit_notes_detail.sort_values(
                            "created",
                            ascending=False
                        )

                        credit_notes_detail = add_hs_identity(
                            credit_notes_detail,
                            customer_id_col="customer_id",
                            customer_name_col="customer_name"
                        )

                        credit_notes_detail["created"] = credit_notes_detail[
                            "created"
                        ].dt.strftime("%Y-%m-%d")

                        credit_notes_detail["amount"] = credit_notes_detail[
                            "amount"
                        ].map(lambda x: f"${x:,.0f}")

                        credit_notes_detail = credit_notes_detail.rename(columns={
                            "created": "Credit Note Date",
                            "customer_name": "Client",
                            "credit_note_number": "Credit Note",
                            "invoice_number": "Invoice",
                            "amount": "Amount",
                            "currency": "Currency"
                        })

                        credit_notes_detail["HS ID"] = credit_notes_detail[
                            "HS ID"
                        ].map(display_hs_value)

                        credit_notes_detail["HS Name"] = credit_notes_detail[
                            "HS Name"
                        ].map(display_hs_value)

                        credit_notes_detail = add_comments_from_sheet(
                            credit_notes_detail,
                            "Credit Notes Detail",
                            "Credit Note"
                        )

                        credit_notes_editor = credit_notes_detail[
                            [
                                "Credit Note Date",
                                "HS ID",
                                "HS Name",
                                "Client",
                                "Credit Note",
                                "Invoice",
                                "Amount",
                                "Currency",
                                "Comments"
                            ]
                        ].copy().reset_index(drop=True)

                        with st.form("credit_notes_detail_comments_form"):
                            edited_credit_notes_detail = st.data_editor(
                                credit_notes_editor,
                                width="stretch",
                                height=420,
                                hide_index=True,
                                key="credit_notes_detail_editor",
                                column_order=[
                                    "HS ID",
                                    "HS Name",
                                    "Credit Note Date",
                                    "Credit Note",
                                    "Invoice",
                                    "Amount",
                                    "Currency",
                                    "Comments"
                                ],
                                column_config={
                                    "HS ID": st.column_config.TextColumn("HS ID", width="small"),
                                    "HS Name": st.column_config.TextColumn("HS Name", width="medium"),
                                    "Credit Note Date": st.column_config.TextColumn("Credit Note Date", width="small"),
                                    "Credit Note": st.column_config.TextColumn("Credit Note", width="small"),
                                    "Invoice": st.column_config.TextColumn("Invoice", width="small"),
                                    "Amount": st.column_config.TextColumn("Amount", width="small"),
                                    "Currency": st.column_config.TextColumn("Currency", width="small"),
                                    "Comments": st.column_config.TextColumn("Comments", width="large")
                                },
                                disabled=[
                                    "HS ID",
                                    "HS Name",
                                    "Credit Note Date",
                                    "Credit Note",
                                    "Invoice",
                                    "Amount",
                                    "Currency"
                                ]
                            )

                            save_credit_notes_comments = st.form_submit_button("Save Comments")

                        if save_credit_notes_comments:
                            save_sheet_comments_batch(
                                "Credit Notes Detail",
                                [
                                    {
                                        "record_key": row["Credit Note"],
                                        "client_name": row["HS Name"],
                                        "comment": row["Comments"]
                                    }
                                    for _, row in edited_credit_notes_detail.iterrows()
                                ]
                            )

                    else:
                        st.info(
                            "No credit notes found for this selection."
                        )

                else:
                    st.info(
                        "No credit notes found for this year."
                    )

            else:
                st.info(
                    "No credit notes found for the selected currency."
                )

        else:
            st.info(
                "No credit notes found in Google Sheets."
            )

        # ==================================================
        # GOOGLE SHEETS REFUNDS
        # ==================================================
        notion_anchor("invoice-refunds")
        st.markdown("### Refunds Issued")

        refunds_df = prepare_refunds_from_google_sheet()

        if len(refunds_df) > 0:

            if selected_currency != "All":
                refunds_df = refunds_df[
                    refunds_df["currency"] == selected_currency
                ].copy()

            if len(refunds_df) > 0:

                refund_year_options = ["All"] + sorted(
                    refunds_df["Year"].dropna().unique().astype(int).tolist(),
                    reverse=True
                )

                refund_filter_col1, refund_filter_col2 = st.columns(2)

                with refund_filter_col1:
                    selected_refund_year = st.selectbox(
                        "Select Refund Year",
                        refund_year_options,
                        index=0,
                        key="selected_refund_year"
                    )

                if selected_refund_year == "All":
                    year_refunds_df = refunds_df.copy()
                else:
                    year_refunds_df = refunds_df[
                        refunds_df["Year"] == selected_refund_year
                    ].copy()

                refund_month_options = ["All"] + (
                    year_refunds_df[["MonthSort", "Month"]]
                    .drop_duplicates()
                    .sort_values("MonthSort", ascending=False)["Month"]
                    .tolist()
                )

                with refund_filter_col2:
                    selected_refund_month = st.selectbox(
                        "Select Refund Month",
                        refund_month_options,
                        index=0,
                        key="selected_refund_month"
                    )

                if selected_refund_month == "All":
                    filtered_refunds_df = year_refunds_df.copy()
                else:
                    filtered_refunds_df = year_refunds_df[
                        year_refunds_df["Month"] == selected_refund_month
                    ].copy()

                refund_count_kpi = int(len(filtered_refunds_df))
                refund_amount_kpi = filtered_refunds_df["amount"].sum()

                refund_card_col1, refund_card_col2 = st.columns(2)

                with refund_card_col1:
                    pro_card(
                        "Succeeded Refunds",
                        format_number(refund_count_kpi),
                        "pro-card-red"
                    )

                with refund_card_col2:
                    pro_card(
                        "Succeeded Refund Amount",
                        format_money(refund_amount_kpi),
                        "pro-card-blue"
                    )

                st.markdown("<br>", unsafe_allow_html=True)

                refund_chart_source_df = year_refunds_df.copy()

                if len(refund_chart_source_df) > 0:

                    refund_chart_df = refund_chart_source_df.groupby(
                        ["MonthSort", "Month"],
                        as_index=False
                    ).agg(
                        Refunds=("refund_id", "count"),
                        Refund_Amount=("amount", "sum")
                    )

                    refund_chart_df = refund_chart_df.sort_values("MonthSort")

                    fig_refunds = go.Figure()

                    fig_refunds.add_trace(
                        go.Bar(
                            x=refund_chart_df["Month"],
                            y=refund_chart_df["Refunds"],
                            name="Refunds",
                            marker_color="rgba(220,38,38,0.34)",
                            width=0.38,
                            text=refund_chart_df["Refunds"].map(label_number),
                            textposition="outside",
                            textfont=CHART_LABEL_FONT,
                            cliponaxis=False,
                            customdata=refund_chart_df["Refund_Amount"],
                            hovertemplate=(
                                "<b>%{x}</b><br>"
                                "Refunds: %{y}<br>"
                                "Amount: $%{customdata:,.0f}"
                                "<extra></extra>"
                            )
                        )
                    )

                    refund_tick_values = refund_chart_df["Month"].tolist()

                    if selected_refund_year == "All" and len(refund_tick_values) > 18:
                        refund_tick_values = refund_tick_values[::3]

                    fig_refunds.update_layout(
                        xaxis_title="Month",
                        yaxis_title="Refunds",
                        xaxis=dict(
                            tickmode="array",
                            tickvals=refund_tick_values
                        )
                    )

                    fig_refunds = pro_chart_layout(fig_refunds, height=380)

                    st.plotly_chart(
                        fig_refunds,
                        width="stretch",
                        key="refunds_issued_chart"
                    )

                    notion_anchor("invoice-refunds-detail")
                    st.markdown("### Refunds Detail")

                    if len(filtered_refunds_df) > 0:

                        refunds_detail = filtered_refunds_df[
                            [
                                "refund_id",
                                "customer_id",
                                "customer_name",
                                "created",
                                "amount",
                                "currency",
                                "status"
                            ]
                        ].copy()

                        refunds_detail = refunds_detail.sort_values(
                            "created",
                            ascending=False
                        )

                        refunds_detail = add_hs_identity(
                            refunds_detail,
                            customer_id_col="customer_id",
                            customer_name_col="customer_name"
                        )

                        refunds_detail["created"] = refunds_detail[
                            "created"
                        ].dt.strftime("%Y-%m-%d")

                        refunds_detail["amount"] = refunds_detail[
                            "amount"
                        ].map(lambda x: f"${x:,.0f}")

                        refunds_detail = refunds_detail.rename(columns={
                            "refund_id": "Refund ID",
                            "customer_name": "Client",
                            "created": "Refund Date",
                            "amount": "Amount",
                            "currency": "Currency",
                            "status": "Status"
                        })

                        refunds_detail["HS ID"] = refunds_detail[
                            "HS ID"
                        ].map(display_hs_value)

                        refunds_detail["HS Name"] = refunds_detail[
                            "HS Name"
                        ].map(display_hs_value)

                        refunds_detail = add_comments_from_sheet(
                            refunds_detail,
                            "Refunds Detail",
                            "Refund ID"
                        )

                        refunds_editor = refunds_detail[
                            [
                                "Refund ID",
                                "HS ID",
                                "HS Name",
                                "Client",
                                "Refund Date",
                                "Amount",
                                "Currency",
                                "Status",
                                "Comments"
                            ]
                        ].copy().reset_index(drop=True)

                        with st.form("refunds_detail_comments_form"):
                            edited_refunds_detail = st.data_editor(
                                refunds_editor,
                                width="stretch",
                                height=420,
                                hide_index=True,
                                key="refunds_detail_editor",
                                column_order=[
                                    "HS ID",
                                    "HS Name",
                                    "Refund Date",
                                    "Amount",
                                    "Currency",
                                    "Status",
                                    "Comments"
                                ],
                                column_config={
                                    "HS ID": st.column_config.TextColumn("HS ID", width="small"),
                                    "HS Name": st.column_config.TextColumn("HS Name", width="medium"),
                                    "Refund Date": st.column_config.TextColumn("Refund Date", width="small"),
                                    "Amount": st.column_config.TextColumn("Amount", width="small"),
                                    "Currency": st.column_config.TextColumn("Currency", width="small"),
                                    "Status": st.column_config.TextColumn("Status", width="small"),
                                    "Comments": st.column_config.TextColumn("Comments", width="large")
                                },
                                disabled=[
                                    "HS ID",
                                    "HS Name",
                                    "Refund Date",
                                    "Amount",
                                    "Currency",
                                    "Status"
                                ]
                            )

                            save_refunds_comments = st.form_submit_button("Save Comments")

                        if save_refunds_comments:
                            save_sheet_comments_batch(
                                "Refunds Detail",
                                [
                                    {
                                        "record_key": row["Refund ID"],
                                        "client_name": row["HS Name"],
                                        "comment": row["Comments"]
                                    }
                                    for _, row in edited_refunds_detail.iterrows()
                                ]
                            )

                    else:
                        st.info(
                            "No succeeded refunds found for this selection."
                        )

                else:
                    st.info(
                        "No succeeded refunds found for this year."
                    )

            else:
                st.info(
                    "No succeeded refunds found for the selected currency."
                )

        else:
            st.info(
                "No succeeded refunds found in Google Sheets."
            )

# ==========================================================
# TAB 5 - STRIPE PAYMENTS
# ==========================================================
if section_is_visible("stripe-payments"):
    tab_context = tab3 if active_section == "all" else st.container()

    with tab_context:

        import numpy as np
        import urllib.parse
        import plotly.express as px
        import plotly.graph_objects as go
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        # ==================================================
        # GOOGLE SHEETS SOURCE
        # ==================================================
        stripe_sheet_id = "114oEoIZLBWxnXbQlm5qcnmGrmT0WBnAJzpQ6Kvb3XIY"
        stripe_sheet_name = "stripe_payments"

        def sheet_url_by_name(sheet_id, sheet_name):
            return (
                f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?"
                f"tqx=out:csv&sheet={urllib.parse.quote(sheet_name)}"
            )

        @st.cache_data(ttl=300, show_spinner=False)
        def load_google_sheet_by_name(sheet_id, sheet_name):
            return load_google_sheet(sheet_id, sheet_name=sheet_name)

        # ==================================================
        # DESIGN
        # ==================================================
        st.markdown("""
        <style>
        .stripe-title {
            font-size:30px;
            font-weight:900;
            color:#0F172A;
            margin-bottom:8px;
            letter-spacing:0;
        }
        .stripe-subtitle {
            color:#64748B;
            font-size:13px;
            font-weight:650;
            margin-bottom:18px;
        }
        .stripe-card {
            background:#FFFFFF;
            padding:14px 16px;
            border-radius:10px;
            border:1px solid #E5E7EB;
            box-shadow:0 3px 12px rgba(15,23,42,.06);
            min-height:88px;
            display:flex;
            flex-direction:column;
            justify-content:center;
        }
        .stripe-card-blue { border-left:5px solid #2563EB; }
        .stripe-card-green { border-left:5px solid #16A34A; }
        .stripe-card-red { border-left:5px solid #DC2626; }
        .stripe-card-amber { border-left:5px solid #D97706; }
        .stripe-card-slate { border-left:5px solid #475569; }
        .stripe-small {
            font-size:12px;
            color:#64748B;
            font-weight:650;
            margin-bottom:8px;
        }
        .stripe-big {
            font-size:24px;
            font-weight:800;
            color:#0F172A;
            line-height:1;
            white-space:nowrap;
        }

        div[data-testid="stDataFrame"] div {
            color:#334155 !important;
            font-weight:400 !important;
            text-shadow:none !important;
            -webkit-font-smoothing:antialiased !important;
        }
        div[data-testid="stDataFrame"] [role="columnheader"] div {
            color:#64748B !important;
            font-weight:500 !important;
            text-shadow:none !important;
        }

        .stripe-static-table-wrap {
            width:100%;
            overflow:auto;
            border:1px solid #E5E7EB;
            border-radius:10px;
            background:#FFFFFF;
        }
        table.stripe-html-table {
            width:100%;
            border-collapse:collapse;
            font-family:Arial, sans-serif;
            font-size:13px;
            color:#334155;
            background:#FFFFFF;
        }
        table.stripe-html-table thead th {
            background:#F8FAFC;
            color:#64748B;
            font-weight:500;
            text-align:left;
            padding:11px 12px;
            border-bottom:1px solid #E5E7EB;
            border-right:1px solid #E5E7EB;
            white-space:nowrap;
        }
        table.stripe-html-table tbody td {
            color:#334155;
            font-weight:400;
            padding:10px 12px;
            border-bottom:1px solid #E5E7EB;
            border-right:1px solid #E5E7EB;
            vertical-align:middle;
        }
        table.stripe-html-table tbody tr:hover td {
            background:#F8FAFC;
        }
        </style>
        """, unsafe_allow_html=True)

        def metric_card(title, value, color_class):
            st.markdown(f"""
            <div class="stripe-card {color_class}">
                <div class="stripe-small">{title}</div>
                <div class="stripe-big">{value}</div>
            </div>
            """, unsafe_allow_html=True)

        def render_static_table(dataframe, height=None):
            table_html = dataframe.to_html(
                index=False,
                classes="stripe-html-table",
                border=0,
                escape=True
            )

            height_style = f"max-height:{height}px;" if height else ""

            st.markdown(
                f"""
                <div class="stripe-static-table-wrap" style="{height_style}">
                    {table_html}
                </div>
                """,
                unsafe_allow_html=True
            )

        def format_money(value):
            try:
                value = float(value)
            except:
                value = 0
            return f"${value:,.0f}"

        def format_money_cents(value):
            try:
                value = float(value)
            except:
                value = 0
            return f"${value:,.2f}"

        def format_percent_no_decimals(value):
            try:
                value = float(value)
            except:
                return "-"
            return f"{value:+.0f}%"

        def format_share_percent(value):
            try:
                value = float(value)
            except:
                value = 0
            return f"{value:.0f}%"

        def to_bool(value):
            return str(value).strip().lower() in ["true", "1", "yes", "y"]

        def short_failure_reason(reason):
            reason = "" if pd.isna(reason) else str(reason).lower().strip()

            if reason == "":
                return "Unknown"
            if "insufficient" in reason or "not enough" in reason:
                return "Insufficient funds"
            if "expired" in reason:
                return "Expired card"
            if "declined" in reason:
                return "Card declined"
            if "cvc" in reason or "security code" in reason:
                return "Incorrect CVC"
            if "authentication" in reason or "authenticate" in reason:
                return "Authentication required"
            if "processing" in reason or "processor" in reason:
                return "Processing error"
            if "do_not_honor" in reason or "do not honor" in reason:
                return "Do not honor"
            if "lost" in reason:
                return "Lost card"
            if "stolen" in reason:
                return "Stolen card"

            return reason.replace("_", " ").title()[:60]

        def aging_bucket(days):
            if pd.isna(days):
                return "0-30"
            if days < 0:
                return "<0"
            if days <= 30:
                return "0-30"
            if days <= 60:
                return "31-60"
            if days <= 90:
                return "61-90"
            return "90+"

        def payment_method_category(method_type):
            method_type = "" if pd.isna(method_type) else str(method_type).lower().strip()

            if method_type == "card":
                return "Card"
            if method_type == "link":
                return "Link"

            transfer_types = [
                "bank_transfer",
                "customer_balance",
                "us_bank_account",
                "ach_credit_transfer",
                "ach_debit",
                "wire_transfer"
            ]

            if method_type in transfer_types or "bank" in method_type or "transfer" in method_type:
                return "Transfer"

            if method_type == "":
                return "Unknown"

            return method_type.replace("_", " ").title()

        def compact_chart_layout(fig, height=330, showlegend=True):
            fig.update_layout(
                height=height,
                plot_bgcolor="#FFFFFF",
                paper_bgcolor="#FFFFFF",
                font=dict(
                    family="Arial",
                    size=12,
                    color="#0F172A"
                ),
                margin=dict(l=24, r=24, t=56, b=64),
                legend=dict(
                    orientation="h",
                    yanchor="bottom",
                    y=1.04,
                    xanchor="right",
                    x=1,
                    font=dict(size=12),
                    traceorder="normal"
                ),
                hoverlabel=dict(
                    bgcolor="#0F172A",
                    font_color="white",
                    font_size=12
                ),
                showlegend=showlegend,
                bargap=0.34
            )

            fig.update_xaxes(
                showgrid=False,
                color="#64748B",
                tickfont=dict(size=11)
            )

            fig.update_yaxes(
                gridcolor="rgba(148,163,184,0.18)",
                zeroline=False,
                color="#64748B",
                tickfont=dict(size=11)
            )

            return fig

        def style_basic_table(dataframe):
            return (
                dataframe.style
                .set_properties(**{
                    "background-color": "#FFFFFF",
                    "color": "#334155",
                    "border-color": "#E5E7EB",
                    "font-weight": "400",
                    "font-size": "13px",
                    "font-family": "Arial"
                })
                .set_table_styles([
                    {
                        "selector": "th",
                        "props": [
                            ("background-color", "#F8FAFC"),
                            ("color", "#64748B"),
                            ("font-weight", "500"),
                            ("border-color", "#E5E7EB"),
                            ("font-size", "13px")
                        ]
                    },
                    {
                        "selector": "td",
                        "props": [
                            ("color", "#334155"),
                            ("font-weight", "400"),
                            ("border-color", "#E5E7EB")
                        ]
                    }
                ])
            )

        def year_options_with_all(dataframe):
            years = sorted(
                dataframe["year"].dropna().astype(int).unique().tolist(),
                reverse=True
            )
            return ["All"] + years

        def year_options_only(dataframe):
            return sorted(
                dataframe["year"].dropna().astype(int).unique().tolist(),
                reverse=True
            )

        def month_options_with_all(dataframe):
            if len(dataframe) == 0:
                return ["All"]

            return ["All"] + (
                dataframe[["month_sort", "month_label"]]
                .drop_duplicates()
                .sort_values("month_sort", ascending=False)["month_label"]
                .tolist()
            )

        def filter_by_year(dataframe, selected_year):
            if selected_year == "All":
                return dataframe.copy()

            return dataframe[
                dataframe["year"] == selected_year
            ].copy()

        def filter_by_month(dataframe, selected_month):
            if selected_month == "All":
                return dataframe.copy()

            return dataframe[
                dataframe["month_label"] == selected_month
            ].copy()

        def add_point_labels(
            fig,
            dataframe,
            x_col,
            y_col,
            labels,
            yshift,
            font_color="#111827",
            border_color="rgba(148,163,184,0.30)"
        ):
            for x_value, y_value, label in zip(dataframe[x_col], dataframe[y_col], labels):
                if pd.isna(y_value):
                    continue

                fig.add_annotation(
                    x=x_value,
                    y=y_value,
                    text=label,
                    showarrow=False,
                    yshift=yshift,
                    font=dict(
                        size=12,
                        color=font_color,
                        family="Arial"
                    ),
                    bgcolor="rgba(255,255,255,0.94)",
                    bordercolor=border_color,
                    borderwidth=1,
                    borderpad=2
                )

        def add_grouped_method_labels(
            fig,
            dataframe,
            method_order,
            value_col,
            month_order,
            method_colors,
            formatter
        ):
            if len(month_order) > 12:
                return 0

            if len(dataframe) == 0:
                return 0

            global_max = dataframe[value_col].max()
            label_gap = max(global_max * 0.10, 1)
            base_label_y = global_max + label_gap
            highest_label = global_max

            for month_index, month in enumerate(month_order):
                month_df = dataframe[
                    dataframe["month_label"] == month
                ].copy()

                if len(month_df) == 0:
                    continue

                lines = []

                for method in method_order[:3]:
                    method_row = month_df[
                        month_df["payment_method_category"] == method
                    ]

                    if len(method_row) == 0:
                        continue

                    value = method_row[value_col].iloc[0]

                    if pd.isna(value) or value <= 0:
                        continue

                    color = method_colors.get(method, "#334155")
                    lines.append(
                        f"<span style='color:{color}'><b>{method}:</b> {formatter(value)}</span>"
                    )

                if not lines:
                    continue

                label_y = base_label_y + (
                    label_gap * 0.18
                    if month_index % 2
                    else 0
                )
                highest_label = max(highest_label, label_y)

                fig.add_annotation(
                    x=month,
                    y=label_y,
                    text="<br>".join(lines),
                    showarrow=False,
                    align="left",
                    yanchor="bottom",
                    font=dict(
                        size=12,
                        color="#334155",
                        family="Arial"
                    ),
                    bgcolor="rgba(255,255,255,0.96)",
                    bordercolor="rgba(148,163,184,0.38)",
                    borderwidth=1,
                    borderpad=4
                )

            return highest_label

        def add_aging_labels_left_of_bars(fig, ag_df, aging_order, aging_colors):
            period_order = ["This Week", "Last Week"]
            totals = ag_df.groupby("period")["amount"].sum()
            max_total = totals.max() if len(totals) > 0 else 0
            label_gap = max(max_total * 0.085, 12000)
            highest_label = max_total

            for period in period_order:
                period_df = ag_df[
                    (ag_df["period"].astype(str) == period) &
                    (ag_df["amount"] > 0)
                ].copy()

                if len(period_df) == 0:
                    continue

                start_y = max_total * 0.92
                label_idx = 0

                for aging in aging_order:
                    temp = period_df[
                        period_df["aging"].astype(str) == aging
                    ]

                    if len(temp) == 0:
                        continue

                    amount = float(temp["amount"].iloc[0])
                    y_label = max(start_y - (label_idx * label_gap), max_total * 0.08)
                    highest_label = max(highest_label, y_label)

                    fig.add_annotation(
                        x=period,
                        y=y_label,
                        xshift=-82,
                        text=format_money(amount),
                        showarrow=False,
                        xanchor="right",
                        yanchor="middle",
                        font=dict(
                            size=12,
                            color=aging_colors.get(aging, "#334155"),
                            family="Arial"
                        ),
                        bgcolor="rgba(255,255,255,0.96)",
                        bordercolor="rgba(148,163,184,0.35)",
                        borderwidth=1,
                        borderpad=2
                    )

                    label_idx += 1

            return highest_label

        # ==================================================
        # LOAD DATA
        # ==================================================
        REQUIRED_COLUMNS = [
            "account_source",
            "charge_id",
            "created",
            "created_ts",
            "amount",
            "currency",
            "customer_id",
            "customer_name",
            "customer_email",
            "invoice_id",
            "invoice_number",
            "due_date",
            "due_date_ts",
            "paid",
            "refunded",
            "charge_status",
            "payment_method_type",
            "payment_method_category",
            "card_brand",
            "failure_code",
            "failure_reason",
            "balance_transaction_id",
            "stripe_fee",
            "net_amount",
            "fee_currency",
            "days_late",
            "aging",
            "pay_date",
            "year",
            "month_num",
            "month_name",
            "month_label",
            "month_sort",
            "description",
            "receipt_email",
            "captured",
            "disputed",
            "last_sync_at"
        ]

        @st.cache_data(ttl=300, show_spinner=False)
        def load_stripe_data_from_sheet():
            stripe_df = load_google_sheet_by_name(
                stripe_sheet_id,
                stripe_sheet_name
            )

            stripe_df.columns = stripe_df.columns.str.strip()

            for col in REQUIRED_COLUMNS:
                if col not in stripe_df.columns:
                    stripe_df[col] = ""

            stripe_df["created"] = pd.to_datetime(
                stripe_df["created"],
                errors="coerce"
            )

            stripe_df["due_date"] = pd.to_datetime(
                stripe_df["due_date"],
                errors="coerce"
            )

            stripe_df["amount"] = pd.to_numeric(
                stripe_df["amount"],
                errors="coerce"
            ).fillna(0)

            stripe_df["stripe_fee"] = pd.to_numeric(
                stripe_df["stripe_fee"],
                errors="coerce"
            ).fillna(0)

            stripe_df["net_amount"] = pd.to_numeric(
                stripe_df["net_amount"],
                errors="coerce"
            ).fillna(0)

            stripe_df["paid"] = stripe_df["paid"].apply(to_bool)
            stripe_df["refunded"] = stripe_df["refunded"].apply(to_bool)
            stripe_df["captured"] = stripe_df["captured"].apply(to_bool)
            stripe_df["disputed"] = stripe_df["disputed"].apply(to_bool)

            stripe_df["currency"] = (
                stripe_df["currency"]
                .astype(str)
                .str.upper()
                .str.strip()
            )

            stripe_df = stripe_df[
                stripe_df["currency"].isin(["USD", ""])
            ].copy()

            stripe_df["charge_status"] = (
                stripe_df["charge_status"]
                .astype(str)
                .str.lower()
                .str.strip()
            )

            stripe_df["payment_method_type"] = (
                stripe_df["payment_method_type"]
                .astype(str)
                .str.lower()
                .str.strip()
            )

            stripe_df["payment_method_category"] = stripe_df.apply(
                lambda row: (
                    payment_method_category(row["payment_method_type"])
                    if str(row["payment_method_category"]).strip() in ["", "nan", "None"]
                    else str(row["payment_method_category"]).strip()
                ),
                axis=1
            )

            stripe_df["card_brand"] = (
                stripe_df["card_brand"]
                .fillna("Not Card")
                .replace("", "Not Card")
                .astype(str)
                .str.title()
            )

            stripe_df["customer_name"] = (
                stripe_df["customer_name"]
                .fillna("")
                .astype(str)
                .str.strip()
            )

            stripe_df["customer_email"] = (
                stripe_df["customer_email"]
                .fillna("")
                .astype(str)
                .str.strip()
            )

            stripe_df["customer_name"] = np.where(
                stripe_df["customer_name"].isin(["", "nan", "None"]),
                stripe_df["customer_email"],
                stripe_df["customer_name"]
            )

            stripe_df["customer_name"] = np.where(
                stripe_df["customer_name"].isin(["", "nan", "None"]),
                "Unknown Customer",
                stripe_df["customer_name"]
            )

            stripe_df["failure_reason"] = (
                stripe_df["failure_reason"]
                .fillna("")
                .astype(str)
                .apply(short_failure_reason)
            )

            stripe_df["days_late"] = pd.to_numeric(
                stripe_df["days_late"],
                errors="coerce"
            )

            computed_days_late = np.where(
                stripe_df["due_date"].notna() & stripe_df["created"].notna(),
                (stripe_df["created"] - stripe_df["due_date"]).dt.days,
                np.nan
            )

            stripe_df["days_late"] = np.where(
                stripe_df["days_late"].notna(),
                stripe_df["days_late"],
                computed_days_late
            )

            stripe_df["aging"] = (
                stripe_df["aging"]
                .fillna("")
                .astype(str)
                .str.strip()
            )

            stripe_df["aging"] = np.where(
                stripe_df["aging"].isin(["", "nan", "None"]),
                pd.Series(stripe_df["days_late"]).apply(aging_bucket),
                stripe_df["aging"]
            )

            stripe_df = stripe_df[
                stripe_df["created"].notna()
            ].copy()

            stripe_df["pay_date"] = stripe_df["created"].dt.date
            stripe_df["year"] = stripe_df["created"].dt.year.astype("Int64")
            stripe_df["month_num"] = stripe_df["created"].dt.month.astype("Int64")
            stripe_df["month_name"] = stripe_df["created"].dt.strftime("%b")
            stripe_df["month_label"] = stripe_df["created"].dt.strftime("%Y-%b")
            stripe_df["month_sort"] = stripe_df["created"].dt.to_period("M")

            stripe_df = stripe_df.drop_duplicates(
                subset=["charge_id"]
            )

            return stripe_df

        try:
            stripe_df = load_stripe_data_from_sheet()
        except Exception as e:
            st.error("Stripe payments data could not be loaded from Google Sheets.")
            st.caption(str(e))
            st.stop()

        # ==================================================
        # HEADER
        # ==================================================
        st.markdown(
            "<div class='stripe-title'>Stripe Payments USA</div>",
            unsafe_allow_html=True
        )

        st.markdown(
            "<div class='stripe-subtitle'>Google Sheets source: stripe_payments - Currency: USD</div>",
            unsafe_allow_html=True
        )

        successful_df = stripe_df[
            (stripe_df["paid"] == True) &
            (stripe_df["refunded"] == False) &
            (stripe_df["charge_status"] == "succeeded")
        ].copy()

        if len(successful_df) == 0:
            st.info("No successful Stripe payments found.")
            st.stop()

        today_colombia = datetime.now(
            ZoneInfo("America/Bogota")
        ).date()

        start_week = today_colombia - timedelta(days=today_colombia.weekday())
        end_week = start_week + timedelta(days=6)

        last_start = start_week - timedelta(days=7)
        last_end = start_week - timedelta(days=1)

        cur = successful_df[
            (successful_df["pay_date"] >= start_week) &
            (successful_df["pay_date"] <= end_week)
        ].copy()

        prev = successful_df[
            (successful_df["pay_date"] >= last_start) &
            (successful_df["pay_date"] <= last_end)
        ].copy()

        card_col1, card_col2 = st.columns(2)

        with card_col1:
            metric_card(
                "This Week",
                format_money(cur["amount"].sum()),
                "stripe-card-blue"
            )

        with card_col2:
            metric_card(
                "Last Week",
                format_money(prev["amount"].sum()),
                "stripe-card-slate"
            )

        st.markdown("<br>", unsafe_allow_html=True)

        # ==================================================
        # COLLECTIONS BY AGING
        # ==================================================
        notion_anchor("stripe-aging")
        st.markdown("#### Collections By Aging")

        wk = pd.concat([
            cur.assign(period="This Week"),
            prev.assign(period="Last Week")
        ])

        aging_order = [
            "<0",
            "0-30",
            "31-60",
            "61-90",
            "90+"
        ]

        aging_colors = {
            "<0": "#059669",
            "0-30": "#2563EB",
            "31-60": "#D97706",
            "61-90": "#EA580C",
            "90+": "#DC2626"
        }

        if len(wk) > 0:

            ag = wk.groupby(
                ["period", "aging"],
                as_index=False
            )["amount"].sum()

            full_aging_index = pd.MultiIndex.from_product(
                [["This Week", "Last Week"], aging_order],
                names=["period", "aging"]
            )

            ag = (
                ag.set_index(["period", "aging"])
                .reindex(full_aging_index, fill_value=0)
                .reset_index()
            )

            ag["period"] = pd.Categorical(
                ag["period"],
                categories=["This Week", "Last Week"],
                ordered=True
            )

            ag["aging"] = pd.Categorical(
                ag["aging"],
                categories=aging_order,
                ordered=True
            )

            ag = ag.sort_values(["period", "aging"])

            aging_chart_col, aging_summary_col = st.columns([3.2, 1])

            with aging_chart_col:
                fig_aging = go.Figure()

                for aging in aging_order:
                    temp_aging = ag[
                        ag["aging"] == aging
                    ].copy()

                    fig_aging.add_trace(
                        go.Bar(
                            x=temp_aging["period"],
                            y=temp_aging["amount"],
                            name=aging,
                            marker_color=aging_colors.get(aging, "#64748B"),
                            width=0.34,
                            hovertemplate=(
                                "<b>%{x}</b><br>"
                                f"{aging}<br>"
                                "$%{y:,.0f}"
                                "<extra></extra>"
                            )
                        )
                    )

                highest_aging_label = add_aging_labels_left_of_bars(
                    fig_aging,
                    ag,
                    aging_order,
                    aging_colors
                )

                fig_aging.update_layout(
                    barmode="stack",
                    xaxis_title="Period",
                    yaxis_title="Amount USD",
                    yaxis=dict(
                        tickprefix="$",
                        separatethousands=True,
                        range=[
                            0,
                            highest_aging_label * 1.12
                            if highest_aging_label > 0
                            else 10
                        ]
                    ),
                    legend_title="Aging",
                    legend_traceorder="normal"
                )

                fig_aging = compact_chart_layout(fig_aging, height=335)
                fig_aging.update_layout(legend_traceorder="normal")

                st.plotly_chart(
                    fig_aging,
                    width="stretch",
                    key="stripe_sheet_collections_by_aging_chart"
                )

            with aging_summary_col:
                st.markdown("##### Aging Summary")

                aging_summary = wk.groupby(
                    "aging",
                    as_index=False
                ).agg(
                    Payments=("charge_id", "nunique"),
                    Amount=("amount", "sum")
                )

                aging_summary = (
                    aging_summary
                    .set_index("aging")
                    .reindex(aging_order, fill_value=0)
                    .reset_index()
                    .rename(columns={"aging": "Aging"})
                )

                total_aging_row = pd.DataFrame([{
                    "Aging": "Total",
                    "Payments": int(aging_summary["Payments"].sum()),
                    "Amount": aging_summary["Amount"].sum()
                }])

                aging_summary = pd.concat(
                    [aging_summary, total_aging_row],
                    ignore_index=True
                )

                aging_summary["Amount"] = aging_summary["Amount"].map(format_money)
                aging_summary["Payments"] = aging_summary["Payments"].astype(int)

                st.dataframe(
                    style_basic_table(aging_summary),
                    width="stretch",
                    height=285,
                    hide_index=True
                )

        else:
            st.info("No weekly collections data available.")

        # ==================================================
        # MONTHLY COLLECTIONS + REPORT YEAR FILTER
        # ==================================================
        notion_anchor("stripe-monthly-collections")
        st.markdown("#### Monthly Collections")

        report_year_options = year_options_with_all(successful_df)

        selected_report_year = st.selectbox(
            "Select Year",
            report_year_options,
            index=0,
            key="stripe_sheet_report_year"
        )

        report_year_df = filter_by_year(
            successful_df,
            selected_report_year
        )

        monthly_df = report_year_df.groupby(
            ["month_sort", "month_label"],
            as_index=False
        )["amount"].sum()

        monthly_df = monthly_df.sort_values("month_sort")

        current_month_period = pd.Period(
            pd.Timestamp(today_colombia),
            freq="M"
        )

        monthly_avg_df = monthly_df[
            monthly_df["month_sort"] < current_month_period
        ].copy()

        total_collected_selected = report_year_df["amount"].sum()
        total_payments_selected = report_year_df["charge_id"].nunique()
        avg_monthly_selected = (
            monthly_avg_df["amount"].mean()
            if len(monthly_avg_df) > 0
            else 0
        )

        monthly_card_col1, monthly_card_col2, monthly_card_col3 = st.columns(3)

        with monthly_card_col1:
            metric_card(
                "Total Collected",
                format_money(total_collected_selected),
                "stripe-card-green"
            )

        with monthly_card_col2:
            metric_card(
                "Payments",
                f"{total_payments_selected:,}",
                "stripe-card-blue"
            )

        with monthly_card_col3:
            metric_card(
                "Avg Monthly",
                format_money(avg_monthly_selected),
                "stripe-card-slate"
            )

        st.markdown("<br>", unsafe_allow_html=True)

        if len(monthly_df) > 0:
            show_labels = len(monthly_df) <= 12

            fig_monthly = go.Figure()

            fig_monthly.add_trace(
                go.Bar(
                    x=monthly_df["month_label"],
                    y=monthly_df["amount"],
                    name="Collections",
                    marker_color="rgba(37,99,235,0.78)",
                    width=0.36,
                    text=monthly_df["amount"].map(format_money) if show_labels else None,
                    textposition="outside" if show_labels else None,
                    textfont=dict(
                        size=13,
                        color="#334155",
                        family="Arial"
                    ),
                    cliponaxis=False,
                    hovertemplate=(
                        "<b>%{x}</b><br>"
                        "Collections: $%{y:,.0f}"
                        "<extra></extra>"
                    )
                )
            )

            monthly_y_max = monthly_df["amount"].max()

            fig_monthly.update_layout(
                xaxis_title="Month",
                yaxis_title="Amount USD",
                yaxis=dict(
                    tickprefix="$",
                    separatethousands=True,
                    range=[0, monthly_y_max * 1.16 if monthly_y_max > 0 else 10]
                )
            )

            fig_monthly = compact_chart_layout(fig_monthly, height=335)

            st.plotly_chart(
                fig_monthly,
                width="stretch",
                key="stripe_sheet_monthly_collections_chart"
            )

            # ==================================================
            # MONTHLY COLLECTIONS VARIATION
            # ==================================================
            st.markdown("#### Monthly Collections Variation vs Previous Month")

            monthly_all_df = successful_df.groupby(
                ["month_sort", "month_label"],
                as_index=False
            )["amount"].sum()

            monthly_all_df = monthly_all_df.sort_values("month_sort")
            monthly_all_df["previous_amount"] = monthly_all_df["amount"].shift(1)

            monthly_all_df["variation_pct"] = np.where(
                monthly_all_df["previous_amount"] > 0,
                (
                    (monthly_all_df["amount"] - monthly_all_df["previous_amount"]) /
                    monthly_all_df["previous_amount"] * 100
                ),
                np.nan
            )

            monthly_all_df["year"] = monthly_all_df["month_sort"].apply(
                lambda x: x.year
            )

            if selected_report_year == "All":
                variation_df = monthly_all_df.copy()
            else:
                variation_df = monthly_all_df[
                    monthly_all_df["year"] == selected_report_year
                ].copy()

            variation_plot_df = variation_df[
                variation_df["variation_pct"].notna()
            ].copy()

            if len(variation_plot_df) > 0:

                show_variation_text = selected_report_year != "All"

                variation_plot_df["display_variation_pct"] = variation_plot_df[
                    "variation_pct"
                ].clip(lower=-100, upper=200)

                variation_plot_df["variation_label"] = variation_plot_df[
                    "variation_pct"
                ].map(format_percent_no_decimals)

                fig_variation = go.Figure()

                fig_variation.add_trace(
                    go.Scatter(
                        x=variation_plot_df["month_label"],
                        y=variation_plot_df["display_variation_pct"],
                        mode="lines+markers+text" if show_variation_text else "lines+markers",
                        name="MoM Variation",
                        text=variation_plot_df["variation_label"] if show_variation_text else None,
                        textposition="top center",
                        textfont=dict(
                            size=12,
                            color="#1D4ED8",
                            family="Arial"
                        ),
                        line=dict(
                            color="#1D4ED8",
                            width=3,
                            shape="spline",
                            smoothing=1.05
                        ),
                        marker=dict(
                            size=8,
                            color="#FFFFFF",
                            line=dict(
                                color="#1D4ED8",
                                width=2.4
                            )
                        ),
                        fill="tozeroy",
                        fillcolor="rgba(37,99,235,0.13)",
                        cliponaxis=False,
                        customdata=variation_plot_df["variation_label"],
                        hovertemplate=(
                            "<b>%{x}</b><br>"
                            "Variation: %{customdata}"
                            "<extra></extra>"
                        )
                    )
                )

                fig_variation.add_hline(
                    y=0,
                    line_width=1,
                    line_dash="dash",
                    line_color="rgba(100,116,139,0.55)"
                )

                fig_variation.update_layout(
                    xaxis_title="Month",
                    yaxis_title="Variation vs Previous Month",
                    xaxis=dict(
                        categoryorder="array",
                        categoryarray=variation_plot_df["month_label"].tolist()
                    ),
                    yaxis=dict(
                        ticksuffix="%",
                        tickformat=".0f",
                        range=[-120, 220]
                    )
                )

                fig_variation = compact_chart_layout(
                    fig_variation,
                    height=355,
                    showlegend=False
                )

                fig_variation.update_layout(
                    margin=dict(l=24, r=24, t=72, b=72)
                )

                st.plotly_chart(
                    fig_variation,
                    width="stretch",
                    key="stripe_sheet_monthly_collections_variation_chart"
                )

            else:
                st.info("No previous-month comparison available for this selection.")

        else:
            st.info("No monthly collections data available.")

        # ==================================================
        # PAYMENTS BY METHOD
        # ==================================================
        notion_anchor("stripe-payments-method")
        st.markdown("#### Payments By Method")

        method_df = report_year_df.groupby(
            ["month_sort", "month_label", "month_num", "payment_method_category"],
            as_index=False
        ).agg(
            Payments=("charge_id", "count"),
            Amount=("amount", "sum")
        )

        if len(method_df) > 0:

            method_df["Month Payments"] = method_df.groupby(
                "month_sort"
            )["Payments"].transform("sum")

            method_df["Month Amount"] = method_df.groupby(
                "month_sort"
            )["Amount"].transform("sum")

            method_df["Payment %"] = np.where(
                method_df["Month Payments"] > 0,
                method_df["Payments"] / method_df["Month Payments"] * 100,
                0
            )

            method_df["Amount %"] = np.where(
                method_df["Month Amount"] > 0,
                method_df["Amount"] / method_df["Month Amount"] * 100,
                0
            )

            method_totals = method_df.groupby(
                "payment_method_category",
                as_index=False
            ).agg(
                Total_Payments=("Payments", "sum"),
                Total_Amount=("Amount", "sum")
            )

            method_order_count = method_totals.sort_values(
                ["Total_Payments", "Total_Amount"],
                ascending=False
            )["payment_method_category"].tolist()

            method_order_amount = method_totals.sort_values(
                ["Total_Amount", "Total_Payments"],
                ascending=False
            )["payment_method_category"].tolist()

            month_order = (
                method_df[["month_sort", "month_label"]]
                .drop_duplicates()
                .sort_values("month_sort")["month_label"]
                .tolist()
            )

            method_colors = {
                "Card": "#2563EB",
                "Transfer": "#DC2626",
                "Link": "#D97706",
                "Unknown": "#64748B"
            }

            count_label_shifts = {
                "Card": 34,
                "Transfer": 34,
                "Link": -34,
                "Unknown": -44
            }

            amount_label_shifts = {
                "Transfer": 34,
                "Card": -34,
                "Link": -42,
                "Unknown": -44
            }

            show_line_labels = len(month_order) <= 12

            st.markdown("##### By Payment Count")

            count_chart_df = method_df[
                method_df["payment_method_category"].isin(method_order_count[:3])
            ].copy()

            fig_count_method = go.Figure()

            for method in method_order_count[:3]:
                temp_method = count_chart_df[
                    count_chart_df["payment_method_category"] == method
                ].sort_values("month_sort")

                fig_count_method.add_trace(
                    go.Scatter(
                        x=temp_method["month_label"],
                        y=temp_method["Payments"],
                        mode="lines+markers",
                        name=method,
                        cliponaxis=False,
                        line=dict(
                            color=method_colors.get(method, "#64748B"),
                            width=2.8,
                            shape="spline",
                            smoothing=1.05
                        ),
                        marker=dict(
                            size=7,
                            color="#FFFFFF",
                            line=dict(
                                color=method_colors.get(method, "#64748B"),
                                width=2
                            )
                        ),
                        hovertemplate=(
                            "<b>%{x}</b><br>"
                            f"{method}: " + "%{y:,} payments"
                            "<extra></extra>"
                        )
                    )
                )

            count_y_max = count_chart_df["Payments"].max()
            count_label_y_max = add_grouped_method_labels(
                fig_count_method,
                count_chart_df,
                method_order_count,
                "Payments",
                month_order,
                method_colors,
                lambda value: f"{int(value):,}"
            ) if show_line_labels else 0

            fig_count_method.update_layout(
                xaxis_title="Month",
                yaxis_title="Payments",
                xaxis=dict(
                    categoryorder="array",
                    categoryarray=month_order
                ),
                yaxis=dict(
                    range=[
                        0,
                        max(
                            count_y_max * 1.45,
                            count_label_y_max * 1.30
                        )
                        if count_y_max > 0
                        else 10
                    ]
                )
            )

            fig_count_method = compact_chart_layout(fig_count_method, height=380)

            st.plotly_chart(
                fig_count_method,
                width="stretch",
                key="stripe_sheet_payment_method_count_line_chart"
            )

            method_df["Count Display"] = method_df.apply(
                lambda row: f"{int(row['Payments']):,} ({row['Payment %']:.0f}%)",
                axis=1
            )

            count_matrix = method_df.pivot_table(
                index="payment_method_category",
                columns="month_label",
                values="Count Display",
                aggfunc="first",
                fill_value="-"
            ).reindex(
                index=method_order_count,
                columns=month_order
            )

            count_matrix.index.name = "Payment Method"

            st.dataframe(
                style_basic_table(count_matrix),
                width="stretch",
                height=220
            )

            st.markdown("##### By Amount Weight")

            amount_chart_df = method_df[
                method_df["payment_method_category"].isin(method_order_amount[:3])
            ].copy()

            fig_amount_method = go.Figure()

            for method in method_order_amount[:3]:
                temp_method = amount_chart_df[
                    amount_chart_df["payment_method_category"] == method
                ].sort_values("month_sort")

                fig_amount_method.add_trace(
                    go.Scatter(
                        x=temp_method["month_label"],
                        y=temp_method["Amount"],
                        mode="lines+markers",
                        name=method,
                        cliponaxis=False,
                        line=dict(
                            color=method_colors.get(method, "#64748B"),
                            width=2.8,
                            shape="spline",
                            smoothing=1.05
                        ),
                        marker=dict(
                            size=7,
                            color="#FFFFFF",
                            line=dict(
                                color=method_colors.get(method, "#64748B"),
                                width=2
                            )
                        ),
                        hovertemplate=(
                            "<b>%{x}</b><br>"
                            f"{method}: " + "$%{y:,.0f}"
                            "<extra></extra>"
                        )
                    )
                )

            amount_y_max = amount_chart_df["Amount"].max()
            amount_label_y_max = add_grouped_method_labels(
                fig_amount_method,
                amount_chart_df,
                method_order_amount,
                "Amount",
                month_order,
                method_colors,
                format_money
            ) if show_line_labels else 0

            fig_amount_method.update_layout(
                xaxis_title="Month",
                yaxis_title="Amount USD",
                yaxis=dict(
                    range=[
                        -(amount_y_max * 0.10) if amount_y_max > 0 else -1,
                        max(
                            amount_y_max * 1.45,
                            amount_label_y_max * 1.30
                        )
                        if amount_y_max > 0
                        else 10
                    ],
                    tickprefix="$",
                    separatethousands=True
                ),
                xaxis=dict(
                    categoryorder="array",
                    categoryarray=month_order,
                    title_standoff=34
                )
            )

            fig_amount_method = compact_chart_layout(fig_amount_method, height=390)

            fig_amount_method.update_layout(
                margin=dict(l=24, r=24, t=64, b=118)
            )

            st.plotly_chart(
                fig_amount_method,
                width="stretch",
                key="stripe_sheet_payment_method_amount_line_chart"
            )

            method_df["Amount Display"] = method_df.apply(
                lambda row: f"${row['Amount']:,.0f} ({row['Amount %']:.0f}%)",
                axis=1
            )

            amount_matrix = method_df.pivot_table(
                index="payment_method_category",
                columns="month_label",
                values="Amount Display",
                aggfunc="first",
                fill_value="-"
            ).reindex(
                index=method_order_amount,
                columns=month_order
            )

            amount_matrix.index.name = "Payment Method"

            st.dataframe(
                style_basic_table(amount_matrix),
                width="stretch",
                height=220
            )

        else:
            st.info("No payment method data available for this selection.")

        # ==================================================
        # PAYMENT METHOD / CARD BRAND SUMMARY
        # ==================================================
        notion_anchor("stripe-brand-summary")
        st.markdown("#### Payment Method / Card Brand Summary")

        brand_filter_col1, brand_filter_col2 = st.columns(2)

        brand_year_options = year_options_with_all(successful_df)

        with brand_filter_col1:
            selected_brand_year = st.selectbox(
                "Select Brand Year",
                brand_year_options,
                index=0,
                key="stripe_sheet_brand_year"
            )

        brand_source_df = filter_by_year(
            successful_df,
            selected_brand_year
        )

        brand_month_options = month_options_with_all(brand_source_df)

        with brand_filter_col2:
            selected_brand_month = st.selectbox(
                "Select Brand Month",
                brand_month_options,
                index=0,
                key="stripe_sheet_brand_month"
            )

        brand_source_df = filter_by_month(
            brand_source_df,
            selected_brand_month
        )

        if len(brand_source_df) > 0:

            brand_source_df["Payment Detail"] = np.where(
                brand_source_df["payment_method_category"] == "Card",
                brand_source_df["card_brand"],
                brand_source_df["payment_method_category"]
            )

            brand_source_df["Payment Detail"] = brand_source_df[
                "Payment Detail"
            ].replace("", "Unknown")

            brand_summary = brand_source_df.groupby(
                "Payment Detail",
                as_index=False
            ).agg(
                Payments=("charge_id", "count"),
                Amount=("amount", "sum")
            )

            total_brand_payments = brand_summary["Payments"].sum()
            total_brand_amount = brand_summary["Amount"].sum()

            brand_summary["Payment %"] = np.where(
                total_brand_payments > 0,
                brand_summary["Payments"] / total_brand_payments * 100,
                0
            )

            brand_summary["Amount %"] = np.where(
                total_brand_amount > 0,
                brand_summary["Amount"] / total_brand_amount * 100,
                0
            )

            brand_summary = brand_summary.sort_values(
                ["Payments", "Amount"],
                ascending=False
            ).reset_index(drop=True)

            brand_summary["Rank"] = brand_summary.index + 1
            brand_summary["Amount"] = brand_summary["Amount"].map(format_money)
            brand_summary["Payment %"] = brand_summary["Payment %"].map(lambda x: f"{x:.0f}%")
            brand_summary["Amount %"] = brand_summary["Amount %"].map(lambda x: f"{x:.0f}%")

            brand_summary = brand_summary.rename(columns={
                "Payment Detail": "Payment Method / Brand",
                "Amount %": "Amount Weight"
            })

            brand_summary = brand_summary[
                [
                    "Rank",
                    "Payment Method / Brand",
                    "Payments",
                    "Payment %",
                    "Amount",
                    "Amount Weight"
                ]
            ]

            st.dataframe(
                style_basic_table(brand_summary),
                width="stretch",
                height=300,
                hide_index=True
            )

        else:
            st.info("No payment method or card brand data available for this selection.")

        # ==================================================
        # TOP PAYMENTS
        # ==================================================
        notion_anchor("stripe-top-payments")
        top_col1, top_col2 = st.columns(2)

        with top_col1:
            st.markdown("#### Top 10 Payments This Week")

            t1 = cur.sort_values(
                "amount",
                ascending=False
            )[["customer_id", "customer_name", "amount", "payment_method_category"]].head(10)

            t1 = add_hs_identity(
                t1,
                customer_id_col="customer_id",
                customer_name_col="customer_name"
            )

            t1["amount"] = t1["amount"].map(format_money)
            t1["HS ID"] = t1["HS ID"].map(display_hs_value)
            t1["HS Name"] = t1["HS Name"].map(display_hs_value)

            t1 = t1.rename(columns={
                "customer_name": "Client",
                "amount": "Amount",
                "payment_method_category": "Method"
            })

            t1 = t1[
                [
                    "HS ID",
                    "HS Name",
                    "Amount",
                    "Method"
                ]
            ]

            render_static_table(t1, height=340)

        with top_col2:
            st.markdown("#### Top 10 Payments Last Week")

            t2 = prev.sort_values(
                "amount",
                ascending=False
            )[["customer_id", "customer_name", "amount", "payment_method_category"]].head(10)

            t2 = add_hs_identity(
                t2,
                customer_id_col="customer_id",
                customer_name_col="customer_name"
            )

            t2["amount"] = t2["amount"].map(format_money)
            t2["HS ID"] = t2["HS ID"].map(display_hs_value)
            t2["HS Name"] = t2["HS Name"].map(display_hs_value)

            t2 = t2.rename(columns={
                "customer_name": "Client",
                "amount": "Amount",
                "payment_method_category": "Method"
            })

            t2 = t2[
                [
                    "HS ID",
                    "HS Name",
                    "Amount",
                    "Method"
                ]
            ]

            render_static_table(t2, height=340)

        # ==================================================
        # PAYMENT SUCCESS RATE
        # ==================================================
        st.markdown("<div style='height:28px;'></div>", unsafe_allow_html=True)
        notion_anchor("stripe-success-rate")
        st.markdown("#### Payment Success Rate")

        success_year_options = year_options_only(stripe_df)

        if len(success_year_options) > 0:

            success_filter_col1, success_filter_col2 = st.columns(2)

            with success_filter_col1:
                selected_success_year = st.selectbox(
                    "Select Success Year",
                    success_year_options,
                    index=0,
                    key="stripe_sheet_success_year"
                )

            success_source_df = stripe_df[
                stripe_df["year"] == selected_success_year
            ].copy()

            success_month_options = month_options_with_all(success_source_df)

            with success_filter_col2:
                selected_success_month = st.selectbox(
                    "Select Success Month",
                    success_month_options,
                    index=0,
                    key="stripe_sheet_success_month"
                )

            st.markdown("<div style='height:12px;'></div>", unsafe_allow_html=True)

            success_source_df = filter_by_month(
                success_source_df,
                selected_success_month
            )

            success_source_df["Payment Result"] = np.where(
                (
                    (success_source_df["paid"] == True) &
                    (success_source_df["charge_status"] == "succeeded")
                ),
                "Succeeded",
                np.where(
                    (
                        (success_source_df["charge_status"] == "failed") |
                        (
                            (success_source_df["paid"] == False) &
                            (success_source_df["failure_reason"] != "")
                        )
                    ),
                    "Failed",
                    "Other"
                )
            )

            attempts_df = success_source_df[
                success_source_df["Payment Result"].isin(["Succeeded", "Failed"])
            ].copy()

            if len(attempts_df) > 0:

                total_attempts = len(attempts_df)
                total_succeeded = len(
                    attempts_df[attempts_df["Payment Result"] == "Succeeded"]
                )
                total_failed = len(
                    attempts_df[attempts_df["Payment Result"] == "Failed"]
                )

                success_rate = (
                    total_succeeded / total_attempts * 100
                ) if total_attempts > 0 else 0

                sr_col1, sr_col2, sr_col3, sr_col4 = st.columns(4)

                with sr_col1:
                    metric_card(
                        "Total Attempts",
                        f"{total_attempts:,}",
                        "stripe-card-slate"
                    )

                with sr_col2:
                    metric_card(
                        "Succeeded",
                        f"{total_succeeded:,}",
                        "stripe-card-green"
                    )

                with sr_col3:
                    metric_card(
                        "Failed",
                        f"{total_failed:,}",
                        "stripe-card-red"
                    )

                with sr_col4:
                    metric_card(
                        "Success Rate",
                        f"{success_rate:.0f}%",
                        "stripe-card-blue"
                    )

                st.markdown("<br>", unsafe_allow_html=True)

                status_df = attempts_df.groupby(
                    ["month_sort", "month_label", "Payment Result"],
                    as_index=False
                )["charge_id"].count().rename(
                    columns={"charge_id": "Payments"}
                )

                status_pivot = status_df.pivot_table(
                    index=["month_sort", "month_label"],
                    columns="Payment Result",
                    values="Payments",
                    aggfunc="sum",
                    fill_value=0
                ).reset_index()

                for col in ["Succeeded", "Failed"]:
                    if col not in status_pivot.columns:
                        status_pivot[col] = 0

                status_pivot["Total Attempts"] = (
                    status_pivot["Succeeded"] +
                    status_pivot["Failed"]
                )

                status_pivot["Success Rate"] = np.where(
                    status_pivot["Total Attempts"] > 0,
                    status_pivot["Succeeded"] /
                    status_pivot["Total Attempts"] * 100,
                    0
                )

                status_pivot["Failed Rate"] = 100 - status_pivot["Success Rate"]

                status_pivot = status_pivot.sort_values(
                    "month_sort",
                    ascending=False
                )

                fig_success = go.Figure()

                fig_success.add_trace(
                    go.Bar(
                        y=status_pivot["month_label"],
                        x=status_pivot["Success Rate"],
                        name="Succeeded",
                        orientation="h",
                        marker_color="#16A34A",
                        text=status_pivot["Success Rate"].map(lambda x: f"{x:.0f}%"),
                        textposition="inside",
                        customdata=status_pivot["Succeeded"],
                        hovertemplate=(
                            "<b>%{y}</b><br>"
                            "Succeeded: %{customdata}<br>"
                            "Success Rate: %{x:.0f}%"
                            "<extra></extra>"
                        )
                    )
                )

                fig_success.add_trace(
                    go.Bar(
                        y=status_pivot["month_label"],
                        x=status_pivot["Failed Rate"],
                        name="Failed",
                        orientation="h",
                        marker_color="#DC2626",
                        text=status_pivot["Failed Rate"].map(lambda x: f"{x:.0f}%"),
                        textposition="inside",
                        customdata=status_pivot["Failed"],
                        hovertemplate=(
                            "<b>%{y}</b><br>"
                            "Failed: %{customdata}<br>"
                            "Failed Rate: %{x:.0f}%"
                            "<extra></extra>"
                        )
                    )
                )

                fig_success.update_layout(
                    height=340,
                    barmode="stack",
                    xaxis_title="Payment Result %",
                    yaxis_title="Month",
                    xaxis=dict(
                        range=[0, 100],
                        ticksuffix="%"
                    ),
                    yaxis=dict(
                        categoryorder="array",
                        categoryarray=status_pivot["month_label"].tolist()[::-1]
                    )
                )

                fig_success = compact_chart_layout(fig_success, height=340)

                st.plotly_chart(
                    fig_success,
                    width="stretch",
                    key="stripe_sheet_payment_success_rate_chart"
                )

                success_table = status_pivot[
                    [
                        "month_label",
                        "Succeeded",
                        "Failed",
                        "Total Attempts",
                        "Success Rate",
                        "Failed Rate"
                    ]
                ].copy()

                success_table["Success Rate"] = success_table["Success Rate"].map(lambda x: f"{x:.0f}%")
                success_table["Failed Rate"] = success_table["Failed Rate"].map(lambda x: f"{x:.0f}%")

                success_table = success_table.rename(columns={
                    "month_label": "Month"
                })

                st.dataframe(
                    style_basic_table(success_table),
                    width="stretch",
                    height=260,
                    hide_index=True
                )

            else:
                st.info("No payment attempt data available for this selection.")

        else:
            st.info("No payment success data available.")

        # ==================================================
        # FAILED PAYMENT REASONS
        # ==================================================
        notion_anchor("stripe-failed-reasons")
        st.markdown("#### Failed Payment Reasons")

        failed_filter_col1, failed_filter_col2 = st.columns(2)

        failed_year_options = year_options_with_all(stripe_df)

        with failed_filter_col1:
            selected_failed_year = st.selectbox(
                "Select Failed Year",
                failed_year_options,
                index=0,
                key="stripe_sheet_failed_reason_year"
            )

        failed_source_df = filter_by_year(
            stripe_df,
            selected_failed_year
        )

        failed_month_options = month_options_with_all(failed_source_df)

        with failed_filter_col2:
            selected_failed_month = st.selectbox(
                "Select Failed Month",
                failed_month_options,
                index=0,
                key="stripe_sheet_failed_reason_month"
            )

        failed_source_df = filter_by_month(
            failed_source_df,
            selected_failed_month
        )

        failed_source_df["Payment Result"] = np.where(
            (
                (failed_source_df["paid"] == True) &
                (failed_source_df["charge_status"] == "succeeded")
            ),
            "Succeeded",
            np.where(
                (
                    (failed_source_df["charge_status"] == "failed") |
                    (
                        (failed_source_df["paid"] == False) &
                        (failed_source_df["failure_reason"] != "")
                    )
                ),
                "Failed",
                "Other"
            )
        )

        failed_year_df = failed_source_df[
            failed_source_df["Payment Result"] == "Failed"
        ].copy()

        if len(failed_year_df) > 0:

            failed_year_df["failure_reason"] = (
                failed_year_df["failure_reason"]
                .replace("", "Unknown")
                .apply(short_failure_reason)
            )

            reason_df = failed_year_df.groupby(
                "failure_reason",
                as_index=False
            ).agg(
                Failed_Payments=("charge_id", "count")
            )

            total_failed_reasons = reason_df["Failed_Payments"].sum()

            reason_df["Percentage"] = np.where(
                total_failed_reasons > 0,
                reason_df["Failed_Payments"] / total_failed_reasons * 100,
                0
            )

            reason_df = reason_df.sort_values(
                "Percentage",
                ascending=False
            ).reset_index(drop=True)

            reason_df["Rank"] = reason_df.index + 1
            reason_df["Percentage"] = reason_df["Percentage"].map(lambda x: f"{x:.0f}%")

            reason_df = reason_df.rename(columns={
                "failure_reason": "Failure Reason",
                "Failed_Payments": "Failed Payments",
                "Percentage": "%"
            })

            reason_df = reason_df[
                [
                    "Rank",
                    "Failure Reason",
                    "Failed Payments",
                    "%"
                ]
            ].head(10)

            st.dataframe(
                style_basic_table(reason_df),
                width="stretch",
                height=320,
                hide_index=True
            )

        else:
            st.info("No failed payments found for this selection.")

        # ==================================================
        # STRIPE FEES BY MONTH
        # ==================================================
        notion_anchor("stripe-fees")
        st.markdown("#### Stripe Fees By Month")

        fee_df = successful_df[
            successful_df["stripe_fee"] > 0
        ].copy()

        if len(fee_df) > 0:

            fee_year_options = year_options_with_all(fee_df)

            fee_filter_col1, fee_filter_col2 = st.columns(2)

            with fee_filter_col1:
                selected_fee_year = st.selectbox(
                    "Select Fee Year",
                    fee_year_options,
                    index=0,
                    key="stripe_sheet_fee_year"
                )

            fee_year_df = filter_by_year(
                fee_df,
                selected_fee_year
            )

            fee_month_options = month_options_with_all(fee_year_df)

            with fee_filter_col2:
                selected_fee_month = st.selectbox(
                    "Select Fee Month",
                    fee_month_options,
                    index=0,
                    key="stripe_sheet_fee_month"
                )

            selected_fee_df = filter_by_month(
                fee_year_df,
                selected_fee_month
            )

            total_fee = selected_fee_df["stripe_fee"].sum()
            total_fee_charges = selected_fee_df["charge_id"].nunique()
            avg_fee = (
                total_fee / total_fee_charges
            ) if total_fee_charges > 0 else 0

            fee_card_col1, fee_card_col2, fee_card_col3 = st.columns(3)

            with fee_card_col1:
                metric_card(
                    "Total Stripe Fees",
                    format_money(total_fee),
                    "stripe-card-red"
                )

            with fee_card_col2:
                metric_card(
                    "Charges With Fees",
                    f"{total_fee_charges:,}",
                    "stripe-card-blue"
                )

            with fee_card_col3:
                metric_card(
                    "Avg Fee Per Charge",
                    f"${avg_fee:,.2f}",
                    "stripe-card-green"
                )

            st.markdown("<br>", unsafe_allow_html=True)

            fee_chart_df = fee_year_df.groupby(
                ["month_sort", "month_label"],
                as_index=False
            ).agg(
                Stripe_Fees=("stripe_fee", "sum"),
                Charges=("charge_id", "nunique")
            )

            fee_chart_df = fee_chart_df.sort_values("month_sort")

            fig_fee = go.Figure()

            show_fee_text = len(fee_chart_df) <= 18

            fig_fee.add_trace(
                go.Bar(
                    x=fee_chart_df["month_label"],
                    y=fee_chart_df["Stripe_Fees"],
                    name="Stripe Fees",
                    marker_color="rgba(220,38,38,0.58)",
                    width=0.34,
                    text=fee_chart_df["Stripe_Fees"].map(format_money) if show_fee_text else None,
                    textposition="outside" if show_fee_text else None,
                    textfont=dict(
                        size=13,
                        color="#334155",
                        family="Arial"
                    ),
                    cliponaxis=False,
                    hovertemplate=(
                        "<b>%{x}</b><br>"
                        "Stripe Fees: $%{y:,.0f}"
                        "<extra></extra>"
                    )
                )
            )

            fee_y_max = fee_chart_df["Stripe_Fees"].max()

            fig_fee.update_layout(
                xaxis_title="Month",
                yaxis_title="Stripe Fees",
                yaxis=dict(
                    tickprefix="$",
                    separatethousands=True,
                    range=[0, fee_y_max * 1.18 if fee_y_max > 0 else 10]
                )
            )

            fig_fee = compact_chart_layout(fig_fee, height=335)

            st.plotly_chart(
                fig_fee,
                width="stretch",
                key="stripe_sheet_fee_monthly_chart"
            )

            # ==================================================
            # TOP 10 STRIPE FEES
            # ==================================================
            notion_anchor("stripe-top-fees")
            st.markdown("#### Top 10 Stripe Fees")

            if len(selected_fee_df) > 0:

                fee_customer_df = add_hs_identity(
                    selected_fee_df.copy(),
                    customer_id_col="customer_id",
                    customer_name_col="customer_name"
                )

                top_fees_df = fee_customer_df.groupby(
                    ["HS ID", "HS Name"],
                    as_index=False
                ).agg(
                    Payment_Amount=("amount", "sum"),
                    Stripe_Fee=("stripe_fee", "sum")
                )

                selected_total_fees = top_fees_df["Stripe_Fee"].sum()

                top_fees_df["Fee %"] = np.where(
                    selected_total_fees > 0,
                    top_fees_df["Stripe_Fee"] / selected_total_fees * 100,
                    0
                )

                top_fees_df = top_fees_df.sort_values(
                    "Fee %",
                    ascending=False
                ).head(10)

                top_fees_df["Payment_Amount"] = top_fees_df["Payment_Amount"].map(format_money)
                top_fees_df["Stripe_Fee"] = top_fees_df["Stripe_Fee"].map(format_money_cents)
                top_fees_df["Fee %"] = top_fees_df["Fee %"].map(format_share_percent)
                top_fees_df["HS ID"] = top_fees_df["HS ID"].map(display_hs_value)
                top_fees_df["HS Name"] = top_fees_df["HS Name"].map(display_hs_value)

                top_fees_df = top_fees_df.rename(columns={
                    "Payment_Amount": "Payment Amount",
                    "Stripe_Fee": "Stripe Fee"
                })

                top_fees_df = top_fees_df[
                    [
                        "HS ID",
                        "HS Name",
                        "Payment Amount",
                        "Stripe Fee",
                        "Fee %"
                    ]
                ]

                render_static_table(
                    top_fees_df,
                    height=360
                )

            else:
                st.info("No Stripe fee details available for this selection.")

        else:
            st.info("No Stripe fee data available yet.")





# ==================================================
# TAB 6 - BRAZIL FINANCE
# ==================================================
if section_is_visible("brazil-finance"):
    tab_context = tab4 if active_section == "all" else st.container()

    with tab_context:

        import unicodedata
        import urllib.parse

        st.markdown("## Brazil Finance")

        # ==================================================
        # HELPERS
        # ==================================================
        def sheet_url_by_name(sheet_id, sheet_name):
            return (
                f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?"
                f"tqx=out:csv&sheet={urllib.parse.quote(sheet_name)}"
            )

        def sheet_url_by_gid(sheet_id, gid):
            return (
                f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?"
                f"format=csv&gid={gid}"
            )

        @st.cache_data(ttl=300, show_spinner=False)
        def load_google_sheet_by_name(sheet_id, sheet_name):
            return load_google_sheet(sheet_id, sheet_name=sheet_name)

        @st.cache_data(ttl=300, show_spinner=False)
        def load_google_sheet_by_gid(sheet_id, gid):
            return load_google_sheet(sheet_id, gid=gid)

        def normalize_text(value):
            value = "" if pd.isna(value) else str(value).strip().lower()
            value = unicodedata.normalize("NFKD", value)
            value = "".join([c for c in value if not unicodedata.combining(c)])
            return value

        def clean_money(value):
            if pd.isna(value):
                return 0.0

            value = str(value).strip()

            if value == "":
                return 0.0

            value = (
                value
                .replace("R$", "")
                .replace("$", "")
                .replace("USD", "")
                .replace("BRL", "")
                .replace(" ", "")
            )

            is_negative = False

            if value.startswith("(") and value.endswith(")"):
                is_negative = True
                value = value.replace("(", "").replace(")", "")

            if value.startswith("-"):
                is_negative = True
                value = value.replace("-", "")

            if "," in value and "." in value and value.rfind(",") > value.rfind("."):
                value = value.replace(".", "").replace(",", ".")
            else:
                value = value.replace(",", "")

            try:
                number = float(value)
            except:
                number = 0.0

            return number * -1 if is_negative else number

        def format_money(value, currency):
            sign = "-" if value < 0 else ""
            value = abs(value)

            if currency == "BRL":
                return f"{sign}R$ {value:,.0f}"

            return f"{sign}${value:,.0f}"

        def format_number(value):
            try:
                return f"{float(value):,.0f}"
            except:
                return ""

        def format_percent_no_decimals(value):
            try:
                return f"{float(value):+.0f}%"
            except:
                return "-"

        def format_request_status(value):
            status = normalize_text(value)

            if (
                "paid" in status or
                "pagado" in status or
                "pago" in status or
                "succeeded" in status or
                "approved" in status
            ):
                return "Paid"

            if (
                "pending" in status or
                "pendiente" in status or
                "pendente" in status
            ):
                return "Pending"

            if status == "":
                return "Pending"

            return str(value).strip().title()

        def style_payment_status(value):
            if value == "Paid":
                return (
                    "background-color:#DCFCE7;"
                    "color:#166534;"
                    "font-weight:800;"
                    "border-radius:8px;"
                    "text-align:center;"
                )

            if value == "Pending":
                return (
                    "background-color:#FEF3C7;"
                    "color:#92400E;"
                    "font-weight:800;"
                    "border-radius:8px;"
                    "text-align:center;"
                )

            return (
                "background-color:#F1F5F9;"
                "color:#334155;"
                "font-weight:700;"
                "border-radius:8px;"
                "text-align:center;"
            )

        def month_name_from_number(month_number):
            month_map = {
                1: "Jan",
                2: "Feb",
                3: "Mar",
                4: "Apr",
                5: "May",
                6: "Jun",
                7: "Jul",
                8: "Aug",
                9: "Sep",
                10: "Oct",
                11: "Nov",
                12: "Dec"
            }

            if pd.isna(month_number):
                return ""

            return month_map.get(int(month_number), "")

        def build_month_fields(dataframe, year_col, month_col, date_col=None):
            dataframe = dataframe.copy()

            if date_col is not None:
                dataframe["Date"] = pd.to_datetime(
                    dataframe[date_col],
                    errors="coerce"
                )

            dataframe["Year"] = pd.to_numeric(
                dataframe[year_col],
                errors="coerce"
            ).astype("Int64")

            dataframe["MonthNum"] = pd.to_numeric(
                dataframe[month_col],
                errors="coerce"
            ).astype("Int64")

            dataframe = dataframe[
                dataframe["Date"].notna() &
                dataframe["Year"].notna() &
                dataframe["MonthNum"].notna() &
                dataframe["MonthNum"].between(1, 12)
            ].copy()

            dataframe["MonthSort"] = pd.to_datetime(
                dataframe["Year"].astype(int).astype(str) + "-" +
                dataframe["MonthNum"].astype(int).astype(str) + "-01",
                errors="coerce"
            ).dt.to_period("M")

            dataframe["MonthLabel"] = dataframe.apply(
                lambda row: f"{int(row['Year'])}-{month_name_from_number(row['MonthNum'])}",
                axis=1
            )

            dataframe = dataframe[
                dataframe["MonthSort"].notna() &
                dataframe["MonthLabel"].notna() &
                (dataframe["MonthLabel"].astype(str).str.lower() != "nan") &
                (dataframe["MonthLabel"].astype(str).str.strip() != "")
            ].copy()

            return dataframe

        def month_filter_options(dataframe):
            if len(dataframe) == 0:
                return ["All"]

            return ["All"] + (
                dataframe[["MonthSort", "MonthLabel"]]
                .drop_duplicates()
                .dropna()
                .sort_values("MonthSort", ascending=False)["MonthLabel"]
                .tolist()
            )

        def build_daily_fx(dataframe):
            fx_df = dataframe[
                (dataframe["Date"].notna()) &
                (dataframe["Amount BRL"].abs() > 0) &
                (dataframe["Amount USD"].abs() > 0)
            ].copy()

            fx_df["DateOnly"] = fx_df["Date"].dt.date

            fx_df["FX Rate"] = (
                fx_df["Amount BRL"].abs() /
                fx_df["Amount USD"].abs()
            )

            daily_fx = fx_df.groupby(
                "DateOnly",
                as_index=False
            )["FX Rate"].median()

            fallback_fx = (
                daily_fx["FX Rate"].median()
                if len(daily_fx) > 0
                else 5.20
            )

            return daily_fx, fallback_fx

        def card_box(title, value, color):
            st.markdown(f"""
            <div style="
                background:#FFFFFF;
                padding:14px 16px;
                border-radius:10px;
                border:1px solid #E5E7EB;
                border-left:5px solid {color};
                box-shadow:0 3px 10px rgba(15,23,42,.06);
                min-height:88px;
                display:flex;
                flex-direction:column;
                justify-content:center;
            ">
                <div style="
                    color:#64748B;
                    font-size:12px;
                    font-weight:800;
                    margin-bottom:9px;
                ">
                    {title}
                </div>
                <div style="
                    color:#0F172A;
                    font-size:25px;
                    font-weight:900;
                    line-height:1;
                    white-space:nowrap;
                ">
                    {value}
                </div>
            </div>
            """, unsafe_allow_html=True)

        def investment_box(title, color):
            st.markdown(f"""
            <div style="
                background:#FFFFFF;
                padding:14px 16px;
                border-radius:10px;
                border:1px solid #E5E7EB;
                border-left:5px solid {color};
                box-shadow:0 3px 10px rgba(15,23,42,.06);
                min-height:88px;
                display:flex;
                flex-direction:column;
                justify-content:center;
            ">
                <div style="
                    color:#64748B;
                    font-size:12px;
                    font-weight:800;
                    margin-bottom:9px;
                ">
                    {title}
                </div>
            </div>
            """, unsafe_allow_html=True)

        def compact_chart_layout(fig, height=310):
            fig.update_layout(
                height=height,
                plot_bgcolor="#FFFFFF",
                paper_bgcolor="#FFFFFF",
                font=dict(
                    family="Arial",
                    size=12,
                    color="#0F172A"
                ),
                margin=dict(l=24, r=24, t=42, b=58),
                legend=dict(
                    orientation="h",
                    yanchor="bottom",
                    y=1.04,
                    xanchor="right",
                    x=1,
                    font=dict(size=12)
                ),
                hoverlabel=dict(
                    bgcolor="#0F172A",
                    font_color="white",
                    font_size=12
                ),
                bargap=0.34
            )

            fig.update_xaxes(
                showgrid=False,
                tickangle=-30,
                color="#64748B",
                tickfont=dict(size=11)
            )

            fig.update_yaxes(
                gridcolor="rgba(148,163,184,0.18)",
                zeroline=False,
                color="#64748B",
                tickfont=dict(size=11)
            )

            return fig

        def show_chart_labels(dataframe):
            return len(dataframe) <= 24

        st.markdown("""
        <style>
        div[data-testid="stNumberInput"] input,
        div[data-testid="stTextInput"] input {
            background: #FFFFFF !important;
            border: 1px solid #E5E7EB !important;
            border-radius: 10px !important;
            min-height: 48px !important;
            color: #0F172A !important;
            font-weight: 800 !important;
            font-size: 20px !important;
        }
        div[data-testid="stNumberInput"] button {
            background: #F8FAFC !important;
            border-radius: 8px !important;
        }
        </style>
        """, unsafe_allow_html=True)

        # ==================================================
        # ITAU BANK PAYMENTS
        # ==================================================
        notion_anchor("brazil-income")
        st.markdown("### Itau Bank Payments")

        itau_sheet_id = "114oEoIZLBWxnXbQlm5qcnmGrmT0WBnAJzpQ6Kvb3XIY"
        itau_sheet_name = "Data_Itau_Banks_payments"

        try:
            itau_df = load_google_sheet_by_name(
                itau_sheet_id,
                itau_sheet_name
            )

            itau_df.columns = itau_df.columns.str.strip()

            day_col = itau_df.columns[0]
            month_col = itau_df.columns[1]
            year_col = itau_df.columns[2]
            date_col = itau_df.columns[3]
            launch_col = itau_df.columns[4]
            brl_col = itau_df.columns[6]
            balance_brl_col = itau_df.columns[7]
            comments_col = itau_df.columns[8]
            usd_col = itau_df.columns[9]
            in_out_col = itau_df.columns[10]
            type_col = itau_df.columns[11]

            itau_df = build_month_fields(
                itau_df,
                year_col,
                month_col,
                date_col
            )

            itau_df["Amount BRL"] = itau_df[brl_col].apply(clean_money)
            itau_df["Amount USD"] = itau_df[usd_col].apply(clean_money)
            itau_df["Balance BRL"] = itau_df[balance_brl_col].apply(clean_money)
            itau_df["Description"] = itau_df[launch_col].astype(str).str.strip()
            itau_df["Comments"] = itau_df[comments_col].astype(str).str.strip()
            itau_df["InOut"] = itau_df[in_out_col].astype(str).str.strip()
            itau_df["Type"] = itau_df[type_col].astype(str).str.strip()
            itau_df["TypeNorm"] = itau_df["Type"].apply(normalize_text)

            itau_df = itau_df[
                itau_df["MonthSort"].notna()
            ].copy()

            bank_year_options = ["All"] + sorted(
                itau_df["Year"].dropna().astype(int).unique().tolist(),
                reverse=True
            )

            daily_fx, fallback_fx = build_daily_fx(itau_df)

            # ==================================================
            # INCOME CHART
            # ==================================================
            st.markdown("#### Income")

            income_filter_col1, income_filter_col2 = st.columns(2)

            with income_filter_col1:
                selected_income_currency = st.selectbox(
                    "Select Income Currency",
                    ["USD", "BRL"],
                    key="bank_income_currency_filter_v7"
                )

            income_amount_col = (
                "Amount USD"
                if selected_income_currency == "USD"
                else "Amount BRL"
            )

            with income_filter_col2:
                selected_income_year = st.selectbox(
                    "Select Income Year",
                    bank_year_options,
                    index=0,
                    key="bank_income_year_filter_v7"
                )

            if selected_income_year == "All":
                income_source_df = itau_df.copy()
            else:
                income_source_df = itau_df[
                    itau_df["Year"] == selected_income_year
                ].copy()

            income_source_df = income_source_df[
                income_source_df["TypeNorm"].str.contains("cobro cliente", na=False)
            ].copy()

            monthly_income = income_source_df.groupby(
                ["MonthSort", "MonthLabel"],
                as_index=False
            )[income_amount_col].sum().rename(columns={income_amount_col: "Income"})

            monthly_income = monthly_income[
                monthly_income["Income"] > 0
            ].copy()

            monthly_income = monthly_income.sort_values("MonthSort")

            if len(monthly_income) > 0:
                show_labels = show_chart_labels(monthly_income)

                fig_income = go.Figure()

                fig_income.add_trace(
                    go.Bar(
                        x=monthly_income["MonthLabel"],
                        y=monthly_income["Income"],
                        name="Income",
                        marker_color="rgba(37,99,235,0.82)",
                        width=0.34,
                        text=monthly_income["Income"].map(
                            lambda x: format_money(x, selected_income_currency)
                        ) if show_labels else None,
                        textposition="outside" if show_labels else None,
                        textfont=dict(
                            size=15,
                            color="#334155",
                            family="Arial"
                        ),
                        cliponaxis=False,
                        hovertemplate=(
                            "<b>%{x}</b><br>"
                            "Income: %{y:,.0f}"
                            "<extra></extra>"
                        )
                    )
                )

                income_y_max = monthly_income["Income"].max()

                fig_income.update_layout(
                    xaxis_title="Month",
                    yaxis_title=f"Income {selected_income_currency}",
                    yaxis=dict(
                        tickprefix="R$ " if selected_income_currency == "BRL" else "$",
                        separatethousands=True,
                        range=[0, income_y_max * 1.18 if income_y_max > 0 else 10]
                    )
                )

                fig_income = compact_chart_layout(fig_income, height=300)

                st.plotly_chart(
                    fig_income,
                    width="stretch",
                    key="bank_income_chart_v7"
                )

                # ==================================================
                # ITAU BANK PAYMENTS VARIATION
                # ==================================================
                notion_anchor("brazil-income-variation")
                st.markdown("#### Itau Bank Payments Variation vs Previous Month")

                income_variation_source_df = itau_df[
                    itau_df["TypeNorm"].str.contains("cobro cliente", na=False)
                ].copy()

                monthly_income_all = income_variation_source_df.groupby(
                    ["MonthSort", "MonthLabel"],
                    as_index=False
                )[income_amount_col].sum().rename(columns={income_amount_col: "Income"})

                monthly_income_all = monthly_income_all[
                    monthly_income_all["Income"] > 0
                ].copy()

                monthly_income_all = monthly_income_all.sort_values("MonthSort")
                monthly_income_all["Previous Income"] = monthly_income_all["Income"].shift(1)

                monthly_income_all["Variation %"] = np.where(
                    monthly_income_all["Previous Income"] > 0,
                    (
                        (monthly_income_all["Income"] - monthly_income_all["Previous Income"]) /
                        monthly_income_all["Previous Income"] * 100
                    ),
                    np.nan
                )

                monthly_income_all["Year"] = monthly_income_all["MonthSort"].apply(
                    lambda x: x.year
                )

                if selected_income_year == "All":
                    income_variation_df = monthly_income_all.copy()
                else:
                    income_variation_df = monthly_income_all[
                        monthly_income_all["Year"] == selected_income_year
                    ].copy()

                income_variation_df = income_variation_df[
                    income_variation_df["Variation %"].notna()
                ].copy()

                if len(income_variation_df) > 0:

                    show_variation_labels = selected_income_year != "All"

                    income_variation_df["Display Variation %"] = income_variation_df[
                        "Variation %"
                    ].clip(lower=-100, upper=200)

                    income_variation_df["Variation Label"] = income_variation_df[
                        "Variation %"
                    ].map(format_percent_no_decimals)

                    fig_income_variation = go.Figure()

                    fig_income_variation.add_trace(
                        go.Scatter(
                            x=income_variation_df["MonthLabel"],
                            y=income_variation_df["Display Variation %"],
                            mode="lines+markers+text" if show_variation_labels else "lines+markers",
                            name="Variation",
                            text=income_variation_df["Variation Label"] if show_variation_labels else None,
                            textposition="top center" if show_variation_labels else None,
                            textfont=dict(
                                size=12,
                                color="#1D4ED8",
                                family="Arial"
                            ),
                            fill="tozeroy",
                            fillcolor="rgba(37,99,235,0.13)",
                            line=dict(
                                color="#1D4ED8",
                                width=3,
                                shape="spline",
                                smoothing=0.85
                            ),
                            marker=dict(
                                size=8,
                                color="#FFFFFF",
                                line=dict(color="#1D4ED8", width=2.4)
                            ),
                            customdata=income_variation_df["Variation Label"],
                            hovertemplate=(
                                "<b>%{x}</b><br>"
                                "Variation: %{customdata}"
                                "<extra></extra>"
                            ),
                            cliponaxis=False
                        )
                    )

                    fig_income_variation.add_hline(
                        y=0,
                        line_width=1,
                        line_dash="dash",
                        line_color="rgba(100,116,139,0.55)"
                    )

                    fig_income_variation.update_layout(
                        xaxis_title="Month",
                        yaxis_title="Variation vs Previous Month",
                        yaxis=dict(
                            ticksuffix="%",
                            tickformat=".0f",
                            range=[-120, 220]
                        ),
                        showlegend=False
                    )

                    fig_income_variation = compact_chart_layout(
                        fig_income_variation,
                        height=320
                    )

                    st.plotly_chart(
                        fig_income_variation,
                        width="stretch",
                        key="bank_income_variation_chart_v7"
                    )

                else:
                    st.info("No previous-month variation available for this selection.")

            else:
                st.info("No income data found for this selection.")

            # Shared filters from Income
            selected_investment_currency = selected_income_currency
            selected_investment_year = selected_income_year
            investment_amount_col = income_amount_col

            selected_cost_currency = selected_income_currency
            selected_cost_year = selected_income_year
            cost_amount_col = income_amount_col

            selected_ratio_currency = selected_income_currency
            selected_ratio_year = selected_income_year
            ratio_amount_col = income_amount_col

            # ==================================================
            # INVESTMENT CHART
            # ==================================================
            notion_anchor("brazil-investment")
            st.markdown("#### Investment")

            if selected_investment_year == "All":
                investment_source_df = itau_df.copy()
            else:
                investment_source_df = itau_df[
                    itau_df["Year"] == selected_investment_year
                ].copy()

            investment_source_df = investment_source_df[
                investment_source_df["TypeNorm"].str.contains("inversion|investimento", na=False)
            ].copy()

            monthly_investment = investment_source_df.groupby(
                ["MonthSort", "MonthLabel"],
                as_index=False
            )[investment_amount_col].sum().rename(columns={investment_amount_col: "Investment"})

            monthly_investment["Investment"] = monthly_investment["Investment"].apply(
                lambda x: -abs(x) if x != 0 else 0
            )

            monthly_investment = monthly_investment[
                monthly_investment["Investment"] != 0
            ].copy()

            monthly_investment = monthly_investment.sort_values("MonthSort")

            if len(monthly_investment) > 0:
                show_labels = show_chart_labels(monthly_investment)

                fig_investment = go.Figure()

                fig_investment.add_trace(
                    go.Bar(
                        x=monthly_investment["MonthLabel"],
                        y=monthly_investment["Investment"],
                        name="Investment",
                        marker_color="rgba(34,197,94,0.82)",
                        width=0.34,
                        text=monthly_investment["Investment"].map(
                            lambda x: format_money(x, selected_investment_currency)
                        ) if show_labels else None,
                        textposition="outside" if show_labels else None,
                        textfont=dict(
                            size=12,
                            color="#334155",
                            family="Arial"
                        ),
                        cliponaxis=False,
                        hovertemplate=(
                            "<b>%{x}</b><br>"
                            "Investment: %{y:,.0f}"
                            "<extra></extra>"
                        )
                    )
                )

                investment_y_min = monthly_investment["Investment"].min()

                fig_investment.update_layout(
                    xaxis_title="Month",
                    yaxis_title=f"Investment {selected_investment_currency}",
                    yaxis=dict(
                        tickprefix="R$ " if selected_investment_currency == "BRL" else "$",
                        separatethousands=True,
                        zeroline=True,
                        zerolinecolor="#CBD5E1",
                        range=[
                            investment_y_min * 1.28 if investment_y_min < 0 else -10,
                            0
                        ]
                    )
                )

                fig_investment = compact_chart_layout(fig_investment, height=330)

                st.plotly_chart(
                    fig_investment,
                    width="stretch",
                    key="bank_investment_chart_v7"
                )

            else:
                st.info("No investment data found for this selection.")

            # ==================================================
            # COSTS EVOLUTION
            # ==================================================
            notion_anchor("brazil-costs")
            st.markdown("#### Brazil Costs Evolution")

            if selected_cost_year == "All":
                cost_source_df = itau_df.copy()
            else:
                cost_source_df = itau_df[
                    itau_df["Year"] == selected_cost_year
                ].copy()

            cost_source_df = cost_source_df[
                (
                    cost_source_df["TypeNorm"].str.contains("costo contador|custo contador|costo - contador", na=False) |
                    cost_source_df["TypeNorm"].str.contains("costo junior|custo junior|costo - junior", na=False) |
                    cost_source_df["TypeNorm"].str.contains("costo brasil|custo brasil", na=False) |
                    cost_source_df["TypeNorm"].str.contains("partners|partner", na=False)
                ) &
                (
                    ~cost_source_df["TypeNorm"].str.contains("costo taxes|costo - taxes|tax|taxes", na=False)
                )
            ].copy()

            monthly_costs = cost_source_df.groupby(
                ["MonthSort", "MonthLabel"],
                as_index=False
            )[cost_amount_col].sum().rename(columns={cost_amount_col: "Costs"})

            monthly_costs["Costs"] = monthly_costs["Costs"].abs()

            monthly_costs = monthly_costs[
                monthly_costs["Costs"] > 0
            ].copy()

            monthly_costs = monthly_costs.sort_values("MonthSort")

            if len(monthly_costs) > 0:

                show_labels = show_chart_labels(monthly_costs)

                fig_costs = go.Figure()

                fig_costs.add_trace(
                    go.Bar(
                        x=monthly_costs["MonthLabel"],
                        y=monthly_costs["Costs"],
                        name="Costs",
                        marker_color="rgba(239,68,68,0.42)",
                        width=0.34,
                        text=monthly_costs["Costs"].map(
                            lambda x: format_money(x, selected_cost_currency)
                        ) if show_labels else None,
                        textposition="outside" if show_labels else None,
                        textfont=dict(
                            size=12,
                            color="#334155",
                            family="Arial"
                        ),
                        cliponaxis=False,
                        hovertemplate=(
                            "<b>%{x}</b><br>"
                            "Costs: %{y:,.0f}"
                            "<extra></extra>"
                        )
                    )
                )

                costs_y_max = monthly_costs["Costs"].max()

                fig_costs.update_layout(
                    xaxis_title="Month",
                    yaxis_title=f"Costs {selected_cost_currency}",
                    yaxis=dict(
                        tickprefix="R$ " if selected_cost_currency == "BRL" else "$",
                        separatethousands=True,
                        range=[0, costs_y_max * 1.18 if costs_y_max > 0 else 10]
                    )
                )

                fig_costs = compact_chart_layout(fig_costs, height=300)

                st.plotly_chart(
                    fig_costs,
                    width="stretch",
                    key="bank_costs_evolution_chart_v7"
                )

            else:
                st.info("No Brazil cost data found for this selection.")

            # ==================================================
            # COSTS AS % OF INCOME
            # ==================================================
            notion_anchor("brazil-cost-ratio")
            st.markdown("#### Costs as % of Income")

            if selected_ratio_year == "All":
                ratio_source_df = itau_df.copy()
            else:
                ratio_source_df = itau_df[
                    itau_df["Year"] == selected_ratio_year
                ].copy()

            ratio_income_df = ratio_source_df[
                ratio_source_df["TypeNorm"].str.contains("cobro cliente", na=False)
            ].copy()

            ratio_cost_df = ratio_source_df[
                (
                    ratio_source_df["TypeNorm"].str.contains("costo contador|custo contador|costo - contador", na=False) |
                    ratio_source_df["TypeNorm"].str.contains("costo junior|custo junior|costo - junior", na=False) |
                    ratio_source_df["TypeNorm"].str.contains("costo brasil|custo brasil", na=False) |
                    ratio_source_df["TypeNorm"].str.contains("partners|partner", na=False)
                ) &
                (
                    ~ratio_source_df["TypeNorm"].str.contains("costo taxes|costo - taxes|tax|taxes", na=False)
                )
            ].copy()

            monthly_income_ratio = ratio_income_df.groupby(
                ["MonthSort", "MonthLabel"],
                as_index=False
            )[ratio_amount_col].sum().rename(columns={ratio_amount_col: "Income"})

            monthly_costs_ratio = ratio_cost_df.groupby(
                ["MonthSort", "MonthLabel"],
                as_index=False
            )[ratio_amount_col].sum().rename(columns={ratio_amount_col: "Costs"})

            monthly_costs_ratio["Costs"] = monthly_costs_ratio["Costs"].abs()

            cost_ratio_df = pd.merge(
                monthly_income_ratio,
                monthly_costs_ratio,
                on=["MonthSort", "MonthLabel"],
                how="outer"
            ).fillna(0)

            cost_ratio_df["Cost % of Income"] = np.where(
                cost_ratio_df["Income"] > 0,
                cost_ratio_df["Costs"] / cost_ratio_df["Income"] * 100,
                0
            )

            cost_ratio_df = cost_ratio_df[
                cost_ratio_df["Cost % of Income"] > 0
            ].copy()

            cost_ratio_df = cost_ratio_df.sort_values("MonthSort")

            if len(cost_ratio_df) > 0:

                show_labels = len(cost_ratio_df) <= 10

                ratio_y_max = cost_ratio_df["Cost % of Income"].max()
                ratio_y_top = ratio_y_max * 1.40 if ratio_y_max > 0 else 10

                fig_ratio = go.Figure()

                fig_ratio.add_trace(
                    go.Scatter(
                        x=cost_ratio_df["MonthLabel"],
                        y=cost_ratio_df["Cost % of Income"],
                        mode="lines+markers+text" if show_labels else "lines+markers",
                        name="Costs / Income",
                        fill="tozeroy",
                        fillcolor="rgba(245,158,11,0.14)",
                        line=dict(
                            color="#B45309",
                            width=3,
                            shape="spline",
                            smoothing=0.75
                        ),
                        marker=dict(
                            size=8,
                            color="#FFFFFF",
                            line=dict(color="#B45309", width=2)
                        ),
                        text=cost_ratio_df["Cost % of Income"].map(
                            lambda x: f"{x:.1f}%"
                        ) if show_labels else None,
                        textposition="top center" if show_labels else None,
                        cliponaxis=False,
                        connectgaps=True,
                        hovertemplate=(
                            "<b>%{x}</b><br>"
                            "Costs / Income: %{y:.2f}%"
                            "<extra></extra>"
                        )
                    )
                )

                fig_ratio.update_layout(
                    xaxis_title="Month",
                    yaxis_title="Costs / Income",
                    yaxis=dict(
                        ticksuffix="%",
                        range=[0, ratio_y_top],
                        fixedrange=True
                    ),
                    showlegend=False
                )

                fig_ratio = compact_chart_layout(fig_ratio, height=320)

                fig_ratio.update_yaxes(
                    range=[0, ratio_y_top],
                    ticksuffix="%",
                    gridcolor="rgba(148,163,184,0.18)",
                    zeroline=False,
                    color="#64748B"
                )

                st.plotly_chart(
                    fig_ratio,
                    width="stretch",
                    key="bank_cost_ratio_chart_v7"
                )

            else:
                st.info("No cost ratio data found.")

            # ==================================================
            # PAYMENT DETAIL
            # ==================================================
            notion_anchor("brazil-payment-detail")
            st.markdown("#### Payment Detail")

            detail_col1, detail_col2 = st.columns(2)

            detail_year_options = ["All"] + sorted(
                itau_df["Year"].dropna().astype(int).unique().tolist(),
                reverse=True
            )

            with detail_col1:
                selected_detail_year = st.selectbox(
                    "Select Detail Year",
                    detail_year_options,
                    index=0,
                    key="bank_detail_year_filter_v7"
                )

            if selected_detail_year == "All":
                detail_year_df = itau_df.copy()
            else:
                detail_year_df = itau_df[
                    itau_df["Year"] == selected_detail_year
                ].copy()

            detail_month_options = month_filter_options(detail_year_df)

            with detail_col2:
                selected_detail_month = st.selectbox(
                    "Select Detail Month",
                    detail_month_options,
                    index=0,
                    key="bank_detail_month_filter_v7"
                )

            if selected_detail_month == "All":
                detail_selected_df = detail_year_df.copy()
            else:
                detail_selected_df = detail_year_df[
                    detail_year_df["MonthLabel"] == selected_detail_month
                ].copy()

            detail_selected_df = detail_selected_df[
                (
                    detail_selected_df["TypeNorm"].str.contains("costo contador|custo contador|costo - contador", na=False) |
                    detail_selected_df["TypeNorm"].str.contains("costo junior|custo junior|costo - junior", na=False) |
                    detail_selected_df["TypeNorm"].str.contains("costo brasil|custo brasil", na=False) |
                    detail_selected_df["TypeNorm"].str.contains("partners|partner", na=False)
                ) &
                (
                    ~detail_selected_df["TypeNorm"].str.contains("costo taxes|costo - taxes|tax|taxes", na=False)
                ) &
                (
                    (detail_selected_df["Amount BRL"] < 0) |
                    (detail_selected_df["Amount USD"] < 0)
                )
            ].copy()

            if len(detail_selected_df) > 0:

                detail_selected_df["Abs Amount"] = detail_selected_df[
                    "Amount USD"
                ].abs()

                detail_selected_df = detail_selected_df.sort_values(
                    ["Date", "Abs Amount"],
                    ascending=[False, False]
                )

                detail_table = detail_selected_df[
                    [
                        "MonthLabel",
                        "Comments",
                        "Type",
                        "Amount BRL",
                        "Amount USD"
                    ]
                ].copy()

                detail_table["Amount BRL"] = detail_table["Amount BRL"].map(
                    lambda x: format_money(x, "BRL")
                )

                detail_table["Amount USD"] = detail_table["Amount USD"].map(
                    lambda x: format_money(x, "USD")
                )

                detail_table = detail_table.rename(columns={
                    "MonthLabel": "Month",
                    "Comments": "Description",
                    "Type": "Cost Type"
                })

                st.dataframe(
                    detail_table,
                    width="stretch",
                    height=340,
                    hide_index=True
                )

            else:
                st.info("No cost payment detail found for this selection.")

            # ==================================================
            # TAXES
            # ==================================================
            notion_anchor("brazil-taxes")
            st.markdown("#### Taxes Paid By Month")

            tax_filter_col1, tax_filter_col2 = st.columns(2)

            with tax_filter_col1:
                selected_tax_currency = st.selectbox(
                    "Select Taxes Currency",
                    ["USD", "BRL"],
                    index=0 if selected_income_currency == "USD" else 1,
                    key="bank_tax_currency_filter_v7"
                )

            tax_amount_col = (
                "Amount USD"
                if selected_tax_currency == "USD"
                else "Amount BRL"
            )

            with tax_filter_col2:
                selected_tax_year = st.selectbox(
                    "Select Taxes Year",
                    bank_year_options,
                    index=bank_year_options.index(selected_income_year)
                    if selected_income_year in bank_year_options
                    else 0,
                    key="bank_tax_year_filter_v7"
                )

            if selected_tax_year == "All":
                tax_source_df = itau_df.copy()
            else:
                tax_source_df = itau_df[
                    itau_df["Year"] == selected_tax_year
                ].copy()

            tax_source_df = tax_source_df[
                tax_source_df["TypeNorm"].str.contains("costo taxes|costo - taxes|tax|taxes", na=False)
            ].copy()

            monthly_taxes = tax_source_df.groupby(
                ["MonthSort", "MonthLabel"],
                as_index=False
            )[tax_amount_col].sum().rename(columns={tax_amount_col: "Taxes"})

            monthly_taxes["Taxes"] = monthly_taxes["Taxes"].abs()

            monthly_taxes = monthly_taxes[
                monthly_taxes["Taxes"] > 0
            ].copy()

            monthly_taxes = monthly_taxes.sort_values("MonthSort")

            if len(monthly_taxes) > 0:

                show_labels = len(monthly_taxes) <= 10
                taxes_y_max = monthly_taxes["Taxes"].max()
                taxes_y_top = taxes_y_max * 1.35 if taxes_y_max > 0 else 10

                fig_taxes = go.Figure()

                fig_taxes.add_trace(
                    go.Scatter(
                        x=monthly_taxes["MonthLabel"],
                        y=monthly_taxes["Taxes"],
                        mode="lines+markers+text" if show_labels else "lines+markers",
                        name="Taxes",
                        fill="tozeroy",
                        fillcolor="rgba(14,165,233,0.18)",
                        line=dict(
                            color="#0369A1",
                            width=3,
                            shape="spline",
                            smoothing=0.75
                        ),
                        marker=dict(
                            size=8,
                            color="#FFFFFF",
                            line=dict(color="#0369A1", width=2)
                        ),
                        text=monthly_taxes["Taxes"].map(
                            lambda x: format_money(x, selected_tax_currency)
                        ) if show_labels else None,
                        textposition="top center" if show_labels else None,
                        textfont=dict(
                            size=12,
                            color="#0369A1",
                            family="Arial"
                        ),
                        cliponaxis=False,
                        connectgaps=True,
                        hovertemplate=(
                            "<b>%{x}</b><br>"
                            "Taxes: %{y:,.0f}"
                            "<extra></extra>"
                        )
                    )
                )

                fig_taxes.update_layout(
                    xaxis_title="Month",
                    yaxis_title=f"Taxes {selected_tax_currency}",
                    yaxis=dict(
                        tickprefix="R$ " if selected_tax_currency == "BRL" else "$",
                        separatethousands=True,
                        range=[0, taxes_y_top],
                        fixedrange=True
                    ),
                    showlegend=False
                )

                fig_taxes = compact_chart_layout(fig_taxes, height=320)

                fig_taxes.update_yaxes(
                    range=[0, taxes_y_top],
                    gridcolor="rgba(148,163,184,0.18)",
                    zeroline=False,
                    color="#64748B"
                )

                st.plotly_chart(
                    fig_taxes,
                    width="stretch",
                    key="bank_taxes_chart_v7"
                )

            else:
                st.info("No taxes data found for this selection.")

            # ==================================================
            # BALANCE + MANUAL INVESTMENT INPUT
            # ==================================================
            notion_anchor("brazil-balance-input")
            st.markdown("#### Balance and Investment Input")

            balance_candidates = itau_df[
                (
                    itau_df["TypeNorm"].str.contains("saldo", na=False)
                ) |
                (
                    itau_df["InOut"].astype(str).apply(normalize_text).str.contains("saldo", na=False)
                ) |
                (
                    itau_df["Description"].astype(str).apply(normalize_text).str.contains("saldo", na=False)
                )
            ].copy()

            balance_candidates = balance_candidates.sort_values("Date")

            if len(balance_candidates) > 0:
                latest_balance = balance_candidates.tail(1).iloc[0]
                latest_balance_brl = latest_balance["Balance BRL"]
                latest_balance_usd = latest_balance["Amount USD"]
            else:
                latest_balance_brl = 0
                latest_balance_usd = 0

            saved_investment_brl = load_saved_dashboard_number(
                "Brazil Finance Inputs",
                "Investment BRL"
            )

            saved_investment_usd = load_saved_dashboard_number(
                "Brazil Finance Inputs",
                "Investment USD"
            )

            if st.session_state.pop("investment_values_saved", False):
                st.success("Investment values saved.")

            balance_col1, balance_col2, balance_col3, balance_col4 = st.columns(4)

            with balance_col1:
                card_box(
                    "Latest Balance BRL",
                    f"R$ {latest_balance_brl:,.0f}",
                    "#16A34A"
                )

            with balance_col2:
                card_box(
                    "Latest Balance USD",
                    f"${latest_balance_usd:,.0f}",
                    "#2563EB"
                )

            with balance_col3:
                card_box(
                    "Investment BRL",
                    f"R$ {saved_investment_brl:,.0f}",
                    "#7C3AED"
                )

            with balance_col4:
                card_box(
                    "Investment USD",
                    f"${saved_investment_usd:,.0f}",
                    "#F59E0B"
                )

            investment_input_col1, investment_input_col2 = st.columns(2)

            with investment_input_col1:
                investment_brl_text = st.text_input(
                    "Investment BRL",
                    value=f"{saved_investment_brl:,.2f}",
                    key="manual_investment_brl_v9"
                )

            with investment_input_col2:
                investment_usd_text = st.text_input(
                    "Investment USD",
                    value=f"{saved_investment_usd:,.2f}",
                    key="manual_investment_usd_v9"
                )

            if st.button("Save Investment Values", key="save_investment_values_v9"):
                investment_brl = parse_flexible_number(
                    investment_brl_text,
                    saved_investment_brl
                )

                investment_usd = parse_flexible_number(
                    investment_usd_text,
                    saved_investment_usd
                )

                save_sheet_comments_batch(
                    "Brazil Finance Inputs",
                    [
                        {
                            "record_key": "Investment BRL",
                            "client_name": "Brazil Finance",
                            "comment": f"{investment_brl:.2f}"
                        },
                        {
                            "record_key": "Investment USD",
                            "client_name": "Brazil Finance",
                            "comment": f"{investment_usd:.2f}"
                        }
                    ]
                )
                st.session_state.investment_values_saved = True
                st.rerun()

        except Exception as e:
            st.error("Brazil finance data could not be loaded.")
            st.caption(str(e))

        # ==================================================
        # PAYMENT REQUESTS
        # ==================================================
        notion_anchor("brazil-payment-requests")
        st.markdown("### Payment Requests")

        payment_request_sheet_id = "1JwBG-K0cdmL0W-EFWHqzJ8ildcUEeCay9-Ul31LWVm0"
        payment_request_sheet_name = "Respuestas de formulario 1"

        try:
            requests_df = load_google_sheet_by_name(
                payment_request_sheet_id,
                payment_request_sheet_name
            )

            requests_df.columns = requests_df.columns.str.strip()

            request_date_series = requests_df.iloc[:, 0]
            request_name_series = requests_df.iloc[:, 2]
            request_currency_series = requests_df.iloc[:, 5]
            request_amount_primary_series = requests_df.iloc[:, 6]
            request_amount_secondary_series = requests_df.iloc[:, 22]
            request_description_series = requests_df.iloc[:, 26]
            request_status_series = requests_df.iloc[:, 28]

            requests_df["Request Date"] = pd.to_datetime(
                request_date_series,
                errors="coerce",
                dayfirst=True
            )

            requests_df = requests_df[
                requests_df["Request Date"].notna()
            ].copy()

            requests_df["DateOnly"] = requests_df["Request Date"].dt.date
            requests_df["Day"] = requests_df["Request Date"].dt.day.astype("Int64")
            requests_df["Month"] = requests_df["Request Date"].dt.month.astype("Int64")
            requests_df["Year"] = requests_df["Request Date"].dt.year.astype("Int64")
            requests_df["MonthSort"] = requests_df["Request Date"].dt.to_period("M")
            requests_df["MonthLabel"] = requests_df["Request Date"].dt.strftime("%Y-%b")
            requests_df["Request Name"] = request_name_series
            requests_df["Request Description"] = request_description_series
            requests_df["Payment Status"] = request_status_series.apply(
                format_request_status
            )

            request_amount_primary = request_amount_primary_series.apply(clean_money)
            request_amount_secondary = request_amount_secondary_series.apply(clean_money)

            requests_df["Request Amount"] = np.where(
                request_amount_primary.abs() > 0,
                request_amount_primary,
                request_amount_secondary
            )

            requests_df["Request Currency"] = (
                request_currency_series
                .astype(str)
                .str.upper()
                .str.strip()
            )

            requests_df = requests_df.merge(
                daily_fx,
                on="DateOnly",
                how="left"
            )

            requests_df["FX Rate"] = requests_df["FX Rate"].fillna(fallback_fx)

            requests_df["Total USD"] = np.where(
                requests_df["Request Currency"].str.contains("BRL|REAL|REAIS", na=False),
                requests_df["Request Amount"] / requests_df["FX Rate"],
                requests_df["Request Amount"]
            )

            requests_df = requests_df[
                requests_df["MonthSort"].notna() &
                requests_df["MonthLabel"].notna() &
                (requests_df["MonthLabel"].astype(str).str.lower() != "nan")
            ].copy()

            request_year_options = ["All"] + sorted(
                requests_df["Year"].dropna().astype(int).unique().tolist(),
                reverse=True
            )

            req_col1, req_col2 = st.columns(2)

            with req_col1:
                selected_request_year = st.selectbox(
                    "Select Request Year",
                    request_year_options,
                    index=0,
                    key="payment_request_year_v7"
                )

            if selected_request_year == "All":
                request_year_df = requests_df.copy()
            else:
                request_year_df = requests_df[
                    requests_df["Year"] == selected_request_year
                ].copy()

            request_month_options = month_filter_options(request_year_df)

            with req_col2:
                selected_request_month = st.selectbox(
                    "Select Request Month",
                    request_month_options,
                    index=0,
                    key="payment_request_month_v7"
                )

            if selected_request_month == "All":
                filtered_requests_df = request_year_df.copy()
            else:
                filtered_requests_df = request_year_df[
                    request_year_df["MonthLabel"] == selected_request_month
                ].copy()

            if len(filtered_requests_df) > 0:

                requests_table = pd.DataFrame({
                    "Day": filtered_requests_df["Day"],
                    "Month": filtered_requests_df["Month"],
                    "Year": filtered_requests_df["Year"],
                    "Name": filtered_requests_df["Request Name"],
                    "Currency": filtered_requests_df["Request Currency"],
                    "Total amount to pay": filtered_requests_df["Request Amount"].map(format_number),
                    "Total USD": filtered_requests_df["Total USD"].map(lambda x: f"${x:,.0f}"),
                    "Description": filtered_requests_df["Request Description"],
                    "Status": filtered_requests_df["Payment Status"]
                })

                requests_table = requests_table.sort_values(
                    ["Year", "Month", "Day"],
                    ascending=False
                )

                def style_payment_status_column(column):
                    return column.map(style_payment_status)

                styled_requests_table = requests_table.style.apply(
                    style_payment_status_column,
                    subset=["Status"]
                )

                st.dataframe(
                    styled_requests_table,
                    width="stretch",
                    height=340,
                    hide_index=True
                )

            else:
                st.info("No payment requests found for this selection.")

        except Exception as e:
            st.error("Payment requests could not be loaded.")
            st.caption(str(e))

        # ==================================================
        # NFSE STATUS
        # ==================================================
        notion_anchor("brazil-nfse")
        st.markdown("### NFSE Status")

        nfse_sheet_id = "1pVpJNAPh-9PnEog4d2Ag1MASUiIXZIOnN2ZX-ySBOkk"
        nfse_gid = "1131504266"

        try:
            nfse_df = load_google_sheet_by_gid(
                nfse_sheet_id,
                nfse_gid
            )

            nfse_df.columns = nfse_df.columns.str.strip()

            nfse_date_col = nfse_df.columns[2]
            nfse_amount_col = nfse_df.columns[6]
            nfse_currency_col = nfse_df.columns[5]
            nfse_status_col = nfse_df.columns[8]
            nfse_col = nfse_df.columns[9]

            nfse_df["Date"] = pd.to_datetime(
                nfse_df[nfse_date_col],
                errors="coerce"
            )

            nfse_df = nfse_df[
                nfse_df["Date"].notna()
            ].copy()

            nfse_df["Amount Due"] = nfse_df[nfse_amount_col].apply(clean_money)
            nfse_df["Currency"] = nfse_df[nfse_currency_col].astype(str).str.upper().str.strip()
            nfse_df["StatusNorm"] = nfse_df[nfse_status_col].apply(normalize_text)
            nfse_df["NFSENorm"] = nfse_df[nfse_col].apply(normalize_text)

            nfse_df = nfse_df[
                ~nfse_df["StatusNorm"].isin(["void", "pending"])
            ].copy()

            nfse_df["Year"] = nfse_df["Date"].dt.year.astype("Int64")
            nfse_df["MonthSort"] = nfse_df["Date"].dt.to_period("M")
            nfse_df["MonthLabel"] = nfse_df["Date"].dt.strftime("%Y-%b")
            nfse_df["DateOnly"] = nfse_df["Date"].dt.date

            nfse_df = nfse_df[
                nfse_df["MonthSort"].notna() &
                nfse_df["MonthLabel"].notna() &
                (nfse_df["MonthLabel"].astype(str).str.lower() != "nan")
            ].copy()

            nfse_df = nfse_df.merge(
                daily_fx,
                on="DateOnly",
                how="left"
            )

            nfse_df["FX Rate"] = nfse_df["FX Rate"].fillna(fallback_fx)

            nfse_df["Amount BRL"] = np.where(
                nfse_df["Currency"].str.lower() == "brl",
                nfse_df["Amount Due"],
                nfse_df["Amount Due"] * nfse_df["FX Rate"]
            )

            nfse_df["Amount USD"] = np.where(
                nfse_df["Currency"].str.lower() == "brl",
                nfse_df["Amount Due"] / nfse_df["FX Rate"],
                nfse_df["Amount Due"]
            )

            nfse_year_options = ["All"] + sorted(
                nfse_df["Year"].dropna().astype(int).unique().tolist(),
                reverse=True
            )

            nfse_col1, nfse_col2 = st.columns(2)

            with nfse_col1:
                selected_nfse_year = st.selectbox(
                    "Select NFSE Year",
                    nfse_year_options,
                    index=0,
                    key="nfse_year_v7"
                )

            if selected_nfse_year == "All":
                nfse_year_df = nfse_df.copy()
            else:
                nfse_year_df = nfse_df[
                    nfse_df["Year"] == selected_nfse_year
                ].copy()

            nfse_month_options = month_filter_options(nfse_year_df)

            with nfse_col2:
                selected_nfse_month = st.selectbox(
                    "Select NFSE Month",
                    nfse_month_options,
                    index=0,
                    key="nfse_month_v7"
                )

            if selected_nfse_month == "All":
                nfse_selected_df = nfse_year_df.copy()
            else:
                nfse_selected_df = nfse_year_df[
                    nfse_year_df["MonthLabel"] == selected_nfse_month
                ].copy()

            total_invoiced = len(nfse_selected_df)

            total_authorized = len(
                nfse_selected_df[nfse_selected_df["NFSENorm"] == "authorized"]
            )

            total_pending = len(
                nfse_selected_df[nfse_selected_df["NFSENorm"] == "pending"]
            )

            pending_pct = (
                total_pending / total_invoiced * 100
                if total_invoiced > 0
                else 0
            )

            nfse_card1, nfse_card2, nfse_card3, nfse_card4 = st.columns(4)

            with nfse_card1:
                card_box("Total Invoiced", f"{total_invoiced:,}", "#2563EB")

            with nfse_card2:
                card_box("Total Sent", f"{total_authorized:,}", "#16A34A")

            with nfse_card3:
                card_box("Total Pending", f"{total_pending:,}", "#F59E0B")

            with nfse_card4:
                card_box("Pending %", f"{pending_pct:.1f}%", "#EF4444")

            st.markdown("<br>", unsafe_allow_html=True)

            nfse_chart_df = nfse_year_df[
                nfse_year_df["NFSENorm"].isin(["authorized", "pending"])
            ].copy()

            nfse_chart_df["NFSE Group"] = np.where(
                nfse_chart_df["NFSENorm"] == "authorized",
                "Authorized",
                "Pending"
            )

            nfse_monthly = nfse_chart_df.groupby(
                ["MonthSort", "MonthLabel", "NFSE Group"],
                as_index=False
            ).agg(
                Amount_BRL=("Amount BRL", "sum"),
                Quantity=("NFSENorm", "count")
            )

            nfse_monthly = nfse_monthly[
                nfse_monthly["Amount_BRL"] > 0
            ].copy()

            nfse_monthly = nfse_monthly.sort_values(
                "MonthSort",
                ascending=False
            )

            if len(nfse_monthly) > 0:
                nfse_month_order_recent = (
                    nfse_monthly[["MonthSort", "MonthLabel"]]
                    .drop_duplicates()
                    .sort_values("MonthSort", ascending=False)["MonthLabel"]
                    .tolist()
                )
                nfse_y_max = nfse_monthly["Amount_BRL"].max()

                fig_nfse = px.bar(
                    nfse_monthly,
                    x="MonthLabel",
                    y="Amount_BRL",
                    color="NFSE Group",
                    barmode="group",
                    custom_data=["Quantity"],
                    color_discrete_map={
                        "Authorized": "#16A34A",
                        "Pending": "#F59E0B"
                    },
                    category_orders={
                        "MonthLabel": nfse_month_order_recent,
                        "NFSE Group": ["Authorized", "Pending"]
                    }
                )

                fig_nfse.update_traces(
                    width=0.26,
                    hovertemplate=(
                        "<b>%{x}</b><br>"
                        "%{fullData.name}: R$ %{y:,.0f}<br>"
                        "Quantity: %{customdata[0]:,}"
                        "<extra></extra>"
                    )
                )

                fig_nfse.update_layout(
                    height=350,
                    plot_bgcolor="#FFFFFF",
                    paper_bgcolor="#FFFFFF",
                    font=dict(
                        family="Arial",
                        size=12,
                        color="#0F172A"
                    ),
                    xaxis_title="Month",
                    yaxis_title="Amount BRL",
                    xaxis=dict(
                        showgrid=False,
                        tickangle=-30,
                        color="#64748B",
                        categoryorder="array",
                        categoryarray=nfse_month_order_recent
                    ),
                    yaxis=dict(
                        tickprefix="R$ ",
                        separatethousands=True,
                        gridcolor="rgba(148,163,184,0.18)",
                        zeroline=False,
                        color="#64748B",
                        range=[
                            0,
                            nfse_y_max * 1.34
                            if nfse_y_max > 0
                            else 10
                        ]
                    ),
                    legend=dict(
                        orientation="h",
                        y=1.08,
                        x=1,
                        xanchor="right",
                        yanchor="bottom"
                    ),
                    margin=dict(l=24, r=24, t=118, b=58),
                    hoverlabel=dict(
                        bgcolor="#0F172A",
                        font_color="white"
                    )
                )

                st.plotly_chart(
                    fig_nfse,
                    width="stretch",
                    key="nfse_status_chart_v7"
                )

            summary_nfse = nfse_year_df.groupby(
                ["MonthSort", "MonthLabel"],
                as_index=False
            ).agg(
                Total_Billed_BRL=("Amount BRL", "sum"),
                Total_Billed_USD=("Amount USD", "sum"),
                Total_Invoiced=("NFSENorm", "count"),
                Total_Sent=("NFSENorm", lambda x: (x == "authorized").sum()),
                Total_Pending=("NFSENorm", lambda x: (x == "pending").sum())
            )

            summary_nfse["Pending %"] = np.where(
                summary_nfse["Total_Invoiced"] > 0,
                summary_nfse["Total_Pending"] /
                summary_nfse["Total_Invoiced"] * 100,
                0
            )

            summary_nfse = summary_nfse.sort_values(
                "MonthSort",
                ascending=False
            )

            summary_nfse["Total_Billed_BRL"] = summary_nfse["Total_Billed_BRL"].map(
                lambda x: f"R$ {x:,.0f}"
            )

            summary_nfse["Total_Billed_USD"] = summary_nfse["Total_Billed_USD"].map(
                lambda x: f"${x:,.0f}"
            )

            summary_nfse["Pending %"] = summary_nfse["Pending %"].map(
                lambda x: f"{x:.0f}%"
            )

            summary_nfse = summary_nfse.rename(columns={
                "MonthLabel": "Month",
                "Total_Billed_BRL": "Total Billed BRL",
                "Total_Billed_USD": "Total Billed USD",
                "Total_Invoiced": "Total Invoiced",
                "Total_Sent": "Total Sent",
                "Total_Pending": "Total Pending"
            })

            summary_nfse = summary_nfse[
                [
                    "Month",
                    "Total Billed BRL",
                    "Total Billed USD",
                    "Total Invoiced",
                    "Total Sent",
                    "Total Pending",
                    "Pending %"
                ]
            ]

            st.dataframe(
                summary_nfse,
                width="stretch",
                height=340,
                hide_index=True
            )

        except Exception as e:
            st.error("NFSE data could not be loaded.")
            st.caption(str(e))
