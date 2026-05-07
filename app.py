import streamlit as st
import os
from dotenv import load_dotenv
import xmlrpc.client
import pandas as pd
from sqlalchemy import create_engine, text
import urllib.parse
from streamlit_webrtc import webrtc_streamer, VideoTransformerBase, WebRtcMode
import cv2
import pyrxing
from PIL import Image
import numpy as np
import threading
import time
import queue

# Load environment variables
load_dotenv()

# Odoo Configuration
ODOO_URL = os.getenv('ODOO_URL')
ODOO_DB = os.getenv('ODOO_DB')
ODOO_USERNAME = os.getenv('ODOO_USERNAME')
ODOO_PASSWORD = os.getenv('ODOO_PASSWORD')

# SQL Server Configuration
TENANT_ID = os.getenv('TENANT_ID')
CLIENT_ID = os.getenv('CLIENT_ID')
CLIENT_SECRET = os.getenv('CLIENT_SECRET')
SQL_ENDPOINT = os.getenv('SQL_ENDPOINT')
DATABASE = os.getenv('DATABASE')
SCHEMA = os.getenv('SCHEMA', 'dbo')

# ---------------------------------------------------------------------------
# Real-time Barcode Scanner (streamlit-webrtc VideoTransformer)
# ---------------------------------------------------------------------------

class BarcodeScanner(VideoTransformerBase):
    """
    Processes video frames in a background thread.
    Detected barcode values are pushed to a thread-safe queue consumed by the UI.
    Draws a green overlay on the frame when a barcode is detected.
    """

    def __init__(self):
        self.result_queue: queue.Queue = queue.Queue(maxsize=1)
        self._last_value: str | None = None
        self._cooldown: float = 0.0          # avoid re-triggering same barcode
        self._lock = threading.Lock()

    def recv(self, frame):
        img = frame.to_ndarray(format="bgr24")

        now = time.monotonic()
        if now >= self._cooldown:
            pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            result = pyrxing.read_barcode(pil_img)

            if result and result.text:
                value = result.text.strip()
                with self._lock:
                    if value != self._last_value:
                        self._last_value = value
                        # Don't block; discard if queue is full (old value)
                        try:
                            self.result_queue.get_nowait()
                        except queue.Empty:
                            pass
                        self.result_queue.put_nowait(value)
                        self._cooldown = now + 3.0   # 3-second cooldown per barcode

                # Draw green success overlay
                h, w = img.shape[:2]
                cv2.rectangle(img, (0, 0), (w, h), (0, 255, 0), 8)
                cv2.putText(
                    img, f"SCANNED: {value[:30]}",
                    (10, h - 20), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (0, 255, 0), 2, cv2.LINE_AA
                )

        return img


# ---------------------------------------------------------------------------
# Odoo Helpers
# ---------------------------------------------------------------------------

@st.cache_resource
def get_odoo_connection():
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        if not uid:
            st.error("❌ Odoo Authentication Failed")
            return None, None
        return uid, models
    except Exception as e:
        st.error(f"❌ Odoo Connection Error: {e}")
        return None, None


def get_product_by_lot(lot_no: str, uid, models):
    try:
        lot = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.lot', 'search_read',
            [[['name', '=', lot_no]]],
            {'fields': ['id', 'name', 'product_id', 'product_qty']}
        )
        if not lot:
            return None

        product_info = lot[0]
        product_name = ""
        selling_price = 0
        product_qty = 0

        if product_info.get('product_id'):
            pid = (
                product_info['product_id'][0]
                if isinstance(product_info['product_id'], (list, tuple))
                else product_info['product_id']
            )
            product = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'product.product', 'search_read',
                [[['id', '=', pid]]],
                {'fields': ['id', 'name', 'list_price', 'standard_price',
                            'default_code', 'qty_available']}
            )
            if product:
                product_name = product[0]['name']
                selling_price = (
                    product_info.get('selling_price', 0)
                    or product[0].get('list_price', 0)
                    or product[0].get('standard_price', 0)
                )
                product_qty = product[0].get('qty_available', 0)

        return {
            'lot_no': lot_no,
            'product_name': product_name,
            'selling_price': float(selling_price),
            'product_qty': float(product_qty),
            'lot_id': product_info['id'],
        }
    except Exception as e:
        st.error(f"Error fetching product: {e}")
        return None


# ---------------------------------------------------------------------------
# SQL / Fabric Helpers
# ---------------------------------------------------------------------------

def get_sql_connection():
    try:
        drivers = [
            "ODBC Driver 18 for SQL Server",
            "ODBC Driver 17 for SQL Server",
            "ODBC Driver 13 for SQL Server",
        ]
        for driver in drivers:
            try:
                conn_str = (
                    f"mssql+pyodbc://{CLIENT_ID}:{urllib.parse.quote(CLIENT_SECRET)}"
                    f"@{SQL_ENDPOINT}/{DATABASE}"
                    f"?driver={urllib.parse.quote(driver)}"
                    f"&encrypt=yes&trustservercertificate=no"
                    f"&Authentication=ActiveDirectoryServicePrincipal"
                )
                engine = create_engine(conn_str)
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                return engine
            except Exception:
                continue
        st.error("❌ No suitable ODBC driver found")
        return None
    except Exception as e:
        st.error(f"❌ SQL Connection Error: {e}")
        return None


def check_table_exists(engine) -> bool:
    try:
        q = text(
            f"SELECT COUNT(*) FROM sys.tables "
            f"WHERE name='Product_data' AND schema_id=SCHEMA_ID('{SCHEMA}')"
        )
        with engine.connect() as conn:
            return conn.execute(q).fetchone()[0] > 0
    except Exception:
        return False


def check_record_exists(engine, lot_no: str) -> bool:
    try:
        q = text(f"SELECT COUNT(*) FROM {SCHEMA}.Product_data WHERE lot_no=:lot_no")
        with engine.connect() as conn:
            return conn.execute(q, {'lot_no': lot_no}).fetchone()[0] > 0
    except Exception as e:
        st.error(f"Error checking existing record: {e}")
        return False


def get_existing_record(engine, lot_no: str):
    try:
        q = text(
            f"SELECT lot_no, product_name, selling_price, mapping_sku, shelf_info, product_qty "
            f"FROM {SCHEMA}.Product_data WHERE lot_no=:lot_no"
        )
        with engine.connect() as conn:
            row = conn.execute(q, {'lot_no': lot_no}).fetchone()
            if row:
                return {
                    'lot_no': row[0], 'product_name': row[1],
                    'selling_price': row[2], 'mapping_sku': row[3],
                    'shelf_info': row[4], 'product_qty': row[5],
                }
        return None
    except Exception as e:
        st.error(f"Error fetching existing record: {e}")
        return None


def save_to_fabric_table(data: dict):
    try:
        engine = get_sql_connection()
        if not engine:
            return False, None

        exists = check_record_exists(engine, data['lot_no'])
        shelf = data.get('shelf_info', '') or ''

        with engine.connect() as conn:
            if exists:
                conn.execute(
                    text(
                        f"UPDATE {SCHEMA}.Product_data "
                        f"SET product_name=:product_name, selling_price=:selling_price, "
                        f"mapping_sku=:mapping_sku, shelf_info=:shelf_info, product_qty=:product_qty "
                        f"WHERE lot_no=:lot_no"
                    ),
                    {
                        'lot_no': data['lot_no'],
                        'product_name': data['product_name'],
                        'selling_price': int(data['selling_price']),
                        'mapping_sku': data['mapping_sku'],
                        'shelf_info': shelf,
                        'product_qty': int(data.get('product_qty', 0)),
                    }
                )
                conn.commit()
                return True, "updated"
            else:
                conn.execute(
                    text(
                        f"INSERT INTO {SCHEMA}.Product_data "
                        f"(lot_no, product_name, selling_price, mapping_sku, shelf_info, product_qty) "
                        f"VALUES (:lot_no,:product_name,:selling_price,:mapping_sku,:shelf_info,:product_qty)"
                    ),
                    {
                        'lot_no': data['lot_no'],
                        'product_name': data['product_name'],
                        'selling_price': int(data['selling_price']),
                        'mapping_sku': data['mapping_sku'],
                        'shelf_info': shelf,
                        'product_qty': int(data.get('product_qty', 0)),
                    }
                )
                conn.commit()
                return True, "inserted"
    except Exception as e:
        st.error(f"❌ Error saving to database: {e}")
        return False, None


def show_existing_data():
    try:
        engine = get_sql_connection()
        if not engine:
            return None
        df = pd.read_sql(
            f"SELECT lot_no, product_name, selling_price, mapping_sku, shelf_info "
            f"FROM {SCHEMA}.Product_data ORDER BY lot_no",
            engine
        )
        return df
    except Exception as e:
        st.error(f"Error fetching data: {e}")
        return None


# ---------------------------------------------------------------------------
# Shared lookup helper — called from both scanner and manual-entry paths
# ---------------------------------------------------------------------------

def do_product_lookup(lot_no: str, uid, models, engine):
    """
    Fetches product from Odoo and pre-fills session state.
    Returns True on success, False on failure.
    """
    lot_no = lot_no.strip()
    if not lot_no:
        return False

    # Avoid re-fetching same lot
    if st.session_state.get('scanned_lot') == lot_no and st.session_state.get('scanned_data'):
        return True

    product_data = get_product_by_lot(lot_no, uid, models)
    if not product_data:
        st.session_state.lookup_error = f"❌ No product found for lot: {lot_no}"
        st.session_state.scanned_data = None
        return False

    # Reset fields for new lot
    st.session_state.mapping_sku_input = ""
    st.session_state.shelf_info_input = ""
    st.session_state.existing_record_data = None

    # Check existing DB record
    if engine:
        existing = get_existing_record(engine, lot_no)
        if existing:
            st.session_state.existing_record_data = existing
            st.session_state.mapping_sku_input = existing.get('mapping_sku', '') or ''
            st.session_state.shelf_info_input = existing.get('shelf_info', '') or ''

    st.session_state.scanned_data = product_data
    st.session_state.scanned_lot = lot_no
    st.session_state.lookup_error = None
    return True


# ---------------------------------------------------------------------------
# Page setup & CSS
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Product Registration", layout="wide")

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Sora:wght@400;600;700&display=swap');

    html, body, [class*="css"] {
        font-family: 'Sora', sans-serif !important;
    }

    .stApp { background: #0f1117; color: #e8eaf6; }

    h1 { font-size: 1.6rem !important; font-weight: 700 !important; letter-spacing: -0.5px; }
    h2, h3 { font-size: 1.15rem !important; font-weight: 600 !important; }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {
        background: #1a1d2e;
        border-radius: 10px;
        padding: 4px;
        gap: 4px;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 8px !important;
        font-size: 13px !important;
        color: #8892b0 !important;
        font-weight: 600;
    }
    .stTabs [aria-selected="true"] {
        background: #3b4cca !important;
        color: #fff !important;
    }

    /* Buttons */
    .stButton > button {
        background: linear-gradient(135deg, #3b4cca, #5c6ef5);
        color: #fff !important;
        border: none !important;
        border-radius: 8px !important;
        font-size: 14px !important;
        font-weight: 600 !important;
        padding: 10px 18px !important;
        transition: opacity 0.2s;
    }
    .stButton > button:hover { opacity: 0.88; }

    /* Inputs */
    .stTextInput input {
        background: #1a1d2e !important;
        border: 1.5px solid #2d3561 !important;
        border-radius: 8px !important;
        color: #e8eaf6 !important;
        font-size: 14px !important;
        font-family: 'JetBrains Mono', monospace !important;
    }
    .stTextInput input:focus { border-color: #5c6ef5 !important; box-shadow: 0 0 0 2px #3b4cca44 !important; }
    .stTextInput label { font-size: 12px !important; color: #8892b0 !important; }

    /* Metrics */
    [data-testid="stMetric"] {
        background: #1a1d2e;
        border: 1px solid #2d3561;
        border-radius: 10px;
        padding: 12px 14px !important;
    }
    [data-testid="stMetricLabel"] { font-size: 11px !important; color: #8892b0 !important; }
    [data-testid="stMetricValue"] { font-size: 1.1rem !important; color: #e8eaf6 !important; font-family: 'JetBrains Mono', monospace !important; }

    /* Alerts */
    .stAlert { font-size: 13px !important; border-radius: 8px !important; }

    /* Dataframe */
    .stDataFrame { font-size: 12px !important; }

    /* Form submit */
    [data-testid="stFormSubmitButton"] > button {
        background: linear-gradient(135deg, #00c9a7, #00a389) !important;
        width: 100% !important;
        font-size: 15px !important;
        padding: 12px !important;
    }

    /* Download button */
    .stDownloadButton > button {
        background: #1a1d2e !important;
        border: 1.5px solid #2d3561 !important;
        color: #8892b0 !important;
        font-size: 13px !important;
    }

    /* Divider */
    hr { border-color: #2d3561 !important; margin: 1rem 0 !important; }

    /* Scan badge */
    .scan-badge {
        display: inline-block;
        background: #00c9a7;
        color: #0f1117;
        border-radius: 20px;
        padding: 3px 12px;
        font-size: 12px;
        font-weight: 700;
        font-family: 'JetBrains Mono', monospace;
        animation: pulse 1.5s infinite;
    }
    @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.55; }
    }
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------

for key, default in {
    'scanned_data': None,
    'scanned_lot': None,
    'existing_record_data': None,
    'lookup_error': None,
    'mapping_sku_input': '',
    'shelf_info_input': '',
    'manual_lot_trigger': '',   # tracks last auto-looked-up manual lot
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


# ---------------------------------------------------------------------------
# App entry
# ---------------------------------------------------------------------------

st.title("📦 Product Registration")
st.markdown("---")

# DB + Odoo connections
engine = get_sql_connection()
if engine:
    if not check_table_exists(engine):
        st.error(f"❌ Table `{SCHEMA}.Product_data` does not exist.")
        st.stop()

uid, models = get_odoo_connection()
if not (uid and models):
    st.error("❌ Failed to connect to Odoo. Check your credentials.")
    st.stop()


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab1, tab2 = st.tabs(["📷 Live Scanner", "⌨️ Manual Entry"])

# ── Tab 1: Real-time WebRTC scanner ────────────────────────────────────────
with tab1:
    st.markdown(
        "Point camera at a barcode — product will load **automatically** once detected."
    )

    scanner = webrtc_streamer(
        key="barcode_scanner",
        mode=WebRtcMode.SENDRECV,
        video_transformer_factory=BarcodeScanner,
        media_stream_constraints={"video": {"facingMode": "environment"}, "audio": False},
        async_transform=True,
    )

    # Poll the scanner's result queue every Streamlit rerun
    if scanner.video_transformer:
        transformer: BarcodeScanner = scanner.video_transformer
        try:
            detected_lot = transformer.result_queue.get_nowait()
        except queue.Empty:
            detected_lot = None

        if detected_lot and detected_lot != st.session_state.scanned_lot:
            st.markdown(
                f'<span class="scan-badge">● DETECTED: {detected_lot}</span>',
                unsafe_allow_html=True
            )
            with st.spinner("Fetching product from Odoo…"):
                ok = do_product_lookup(detected_lot, uid, models, engine)
            if ok:
                st.success(f"✅ Auto-fetched: {st.session_state.scanned_data['product_name']}")
            else:
                st.error(st.session_state.lookup_error or "❌ Product not found")
            # Force a rerun so the product details section below refreshes
            st.rerun()

    st.caption(
        "💡 Tip: The scanner uses your rear camera. Make sure lighting is good and "
        "hold the barcode steady for ~1 second."
    )

# ── Tab 2: Manual Entry — auto-fetch on Enter (no button needed) ────────────
with tab2:
    st.subheader("⌨️ Lot Number Entry")

    def _on_manual_lot_change():
        """Called when the text input value changes (on Enter / focus-out)."""
        lot = st.session_state.get('_manual_lot_input_widget', '').strip()
        if not lot:
            return
        # Avoid duplicate lookups
        if lot == st.session_state.manual_lot_trigger:
            return
        st.session_state.manual_lot_trigger = lot
        do_product_lookup(lot, uid, models, engine)

    st.text_input(
        "Enter Lot Number (press Enter to auto-fetch):",
        key="_manual_lot_input_widget",
        placeholder="Scan with barcode gun or type manually…",
        on_change=_on_manual_lot_change,
    )

    # Show inline feedback for manual entry
    if st.session_state.lookup_error and not st.session_state.scanned_data:
        st.error(st.session_state.lookup_error)
    elif st.session_state.scanned_data:
        st.success(f"✅ Auto-fetched: {st.session_state.scanned_data['product_name']}")


# ---------------------------------------------------------------------------
# Product Details + Save Form (shared by both tabs)
# ---------------------------------------------------------------------------

st.markdown("---")
st.subheader("📝 Product Details")

if st.session_state.scanned_data:
    product = st.session_state.scanned_data

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("🏷️ Lot Number", product['lot_no'])
    with col2:
        name = product['product_name']
        st.metric("📦 Product", name[:28] + "…" if len(name) > 28 else name)
    with col3:
        st.metric("💰 Selling Price", f"₹{product['selling_price']:,.2f}")

    if st.session_state.existing_record_data:
        st.info(
            f"📝 **Updating existing record** — "
            f"Current SKU: `{st.session_state.existing_record_data.get('mapping_sku', 'N/A')}`"
        )

    with st.form("product_registration_form"):
        st.subheader("➕ Additional Information")

        mapping_sku = st.text_input(
            "📌 Mapping SKU *",
            placeholder="Enter mapping SKU…",
            help="Required",
            key="mapping_sku_input",
        )

        shelf_info = st.text_input(
            "📍 Shelf Info (optional)",
            placeholder="e.g. A3-Row2…",
            key="shelf_info_input",
        )

        label = "🔄 Update Database" if st.session_state.existing_record_data else "💾 Save to Database"
        submitted = st.form_submit_button(label, type="primary", use_container_width=True)

        if submitted:
            if not mapping_sku:
                st.warning("⚠️ Mapping SKU is required.")
            else:
                save_data = {
                    'lot_no': product['lot_no'],
                    'product_name': product['product_name'],
                    'selling_price': product['selling_price'],
                    'mapping_sku': mapping_sku,
                    'shelf_info': shelf_info or '',
                    'product_qty': product['product_qty'],
                }
                with st.spinner("Saving…"):
                    ok, action = save_to_fabric_table(save_data)
                if ok:
                    verb = "updated" if action == "updated" else "saved"
                    st.success(f"✅ Product data {verb} successfully!")
                    # Clear state
                    for k in ('scanned_data', 'scanned_lot', 'existing_record_data',
                              'lookup_error', 'manual_lot_trigger'):
                        st.session_state[k] = None
                    st.session_state.mapping_sku_input = ''
                    st.session_state.shelf_info_input = ''
                    st.rerun()
                else:
                    st.error("❌ Failed to save data.")

else:
    st.info("👆 Scan a barcode or enter a lot number above to load product details.")


# ---------------------------------------------------------------------------
# Existing Records Table
# ---------------------------------------------------------------------------

st.markdown("---")
st.subheader("📊 Existing Product Records")

col_r, _ = st.columns([1, 5])
with col_r:
    if st.button("🔄 Refresh"):
        st.rerun()

df = show_existing_data()
if df is not None and not df.empty:
    st.dataframe(
        df,
        use_container_width=True,
        column_config={
            "lot_no": "🏷️ Lot No.",
            "product_name": "📦 Product Name",
            "selling_price": st.column_config.NumberColumn("💰 Price (₹)", format="₹%d"),
            "mapping_sku": "📌 Mapping SKU",
            "shelf_info": "📍 Shelf Info",
        },
        hide_index=True,
    )
    csv = df.to_csv(index=False)
    st.download_button(
        "📥 Download CSV",
        data=csv,
        file_name="product_data_export.csv",
        mime="text/csv",
        use_container_width=True,
    )
    st.info(f"📊 Total records: {len(df)}")
else:
    st.info("No records yet. Scan and save your first product!")

# Footer
st.markdown("---")
st.markdown("""
**Instructions:**
1. **Live Scanner** — point camera at barcode; product auto-loads once detected (3-second cooldown between scans).
2. **Manual Entry** — type/scan lot number and press **Enter**; product auto-loads instantly.
3. Fill in **Mapping SKU** (required) and **Shelf Info** (optional).
4. Click **Save / Update** — existing lot numbers are updated, new ones are inserted.
""")
