import streamlit as st
import os
from dotenv import load_dotenv
import xmlrpc.client
import pandas as pd
from sqlalchemy import create_engine, text
import urllib.parse
from streamlit_webrtc import webrtc_streamer, VideoTransformerBase, WebRtcMode
import cv2
# from pyzbar.pyzbar import decode
import pyrxing
from PIL import Image
import numpy as np

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

# Barcode Scanner Class
class BarcodeScanner(VideoTransformerBase):
    def __init__(self):
        self.barcode_data = None
        
    def recv(self, frame):
        img = frame.to_ndarray(format="bgr24")
        
        pil_image = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        result = pyrxing.read_barcode(pil_image)
        
        if result:
            self.barcode_data = result.text
        
        return img
        
# Odoo Connection
@st.cache_resource
def get_odoo_connection():
    """Establish connection to Odoo"""
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
        
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        if not uid:
            st.error("❌ Odoo Authentication Failed")
            return None, None
            
        return uid, models
    except Exception as e:
        st.error(f"❌ Odoo Connection Error: {str(e)}")
        return None, None

def get_product_by_lot(lot_no, uid, models):
    """Fetch product details from Odoo using lot number from stock.lot model"""
    try:
        # Search in stock.lot model
        lot = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.lot', 'search_read',
            [[['name', '=', lot_no]]],
            {'fields': ['id', 'name', 'product_id', 'product_qty']}
        )
        
        if lot:
            # Extract product details
            product_info = lot[0]
            product_name = ""
            selling_price = 0
            product_qty = 0
            
            # Get product name from product_id
            if product_info.get('product_id'):
                product_id = product_info['product_id'][0] if isinstance(product_info['product_id'], (list, tuple)) else product_info['product_id']
                
                product = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    'product.product', 'search_read',
                    [[['id', '=', product_id]]],
                    {'fields': ['id', 'name', 'list_price', 'standard_price', 'default_code', 'qty_available']}
                )
                
                if product:
                    product_name = product[0]['name']
                    selling_price = product_info.get('selling_price', 0) or product[0].get('list_price', 0) or product[0].get('standard_price', 0)
                    # Get quantity from product if not available in lot
                    product_qty = product[0].get('qty_available', 0)
            
            return {
                'lot_no': lot_no,
                'product_name': product_name,
                'selling_price': float(selling_price),
                'product_qty': float(product_qty),
                'lot_id': product_info['id']
            }
        
        return None
        
    except Exception as e:
        st.error(f"Error fetching product: {str(e)}")
        return None

def get_sql_connection():
    """Create SQL Alchemy connection for Fabric/SQL Server"""
    try:
        drivers = ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server", "ODBC Driver 13 for SQL Server"]
        
        for driver in drivers:
            try:
                connection_string = (
                    f"mssql+pyodbc://{CLIENT_ID}:{urllib.parse.quote(CLIENT_SECRET)}@{SQL_ENDPOINT}"
                    f"/{DATABASE}?driver={urllib.parse.quote(driver)}&encrypt=yes&trustservercertificate=no"
                    f"&Authentication=ActiveDirectoryServicePrincipal"
                )
                
                engine = create_engine(connection_string)
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                return engine
            except Exception as e:
                continue
        
        st.error("❌ No suitable ODBC driver found")
        return None
        
    except Exception as e:
        st.error(f"❌ SQL Connection Error: {str(e)}")
        return None

def check_record_exists(engine, lot_no):
    """Check if a record with given lot number already exists"""
    try:
        query = text(f"""
            SELECT COUNT(*) as count 
            FROM {SCHEMA}.Product_data 
            WHERE lot_no = :lot_no
        """)
        with engine.connect() as conn:
            result = conn.execute(query, {'lot_no': lot_no})
            count = result.fetchone()[0]
            return count > 0
    except Exception as e:
        st.error(f"Error checking existing record: {str(e)}")
        return False

def get_existing_record(engine, lot_no):
    """Get existing record data for a lot number"""
    try:
        query = text(f"""
            SELECT lot_no, product_name, selling_price, mapping_sku, shelf_info, product_qty
            FROM {SCHEMA}.Product_data 
            WHERE lot_no = :lot_no
        """)
        with engine.connect() as conn:
            result = conn.execute(query, {'lot_no': lot_no})
            row = result.fetchone()
            if row:
                return {
                    'lot_no': row[0],
                    'product_name': row[1],
                    'selling_price': row[2],
                    'mapping_sku': row[3],
                    'shelf_info': row[4],
                    'product_qty': row[5]
                }
            return None
    except Exception as e:
        st.error(f"Error fetching existing record: {str(e)}")
        return None

def save_to_fabric_table(data):
    """Save or update product data to Fabric table"""
    try:
        engine = get_sql_connection()
        if not engine:
            return False, None
        
        # Check if record exists
        exists = check_record_exists(engine, data['lot_no'])
        
        with engine.connect() as conn:
            shelf_info_value = data.get('shelf_info', '') or ''
            
            if exists:
                # Update existing record (without updated_at)
                update_sql = text(f"""
                    UPDATE {SCHEMA}.Product_data 
                    SET product_name = :product_name,
                        selling_price = :selling_price,
                        mapping_sku = :mapping_sku,
                        shelf_info = :shelf_info,
                        product_qty = :product_qty
                    WHERE lot_no = :lot_no
                """)
                
                conn.execute(update_sql, {
                    'lot_no': data['lot_no'],
                    'product_name': data['product_name'],
                    'selling_price': int(data['selling_price']),
                    'mapping_sku': data['mapping_sku'],
                    'shelf_info': shelf_info_value,
                    'product_qty': int(data.get('product_qty', 0))
                })
                conn.commit()
                return True, "updated"
            else:
                # Insert new record (without created_at)
                insert_sql = text(f"""
                    INSERT INTO {SCHEMA}.Product_data (lot_no, product_name, selling_price, mapping_sku, shelf_info, product_qty)
                    VALUES (:lot_no, :product_name, :selling_price, :mapping_sku, :shelf_info, :product_qty)
                """)
                
                conn.execute(insert_sql, {
                    'lot_no': data['lot_no'],
                    'product_name': data['product_name'],
                    'selling_price': int(data['selling_price']),
                    'mapping_sku': data['mapping_sku'],
                    'shelf_info': shelf_info_value,
                    'product_qty': int(data.get('product_qty', 0))
                })
                conn.commit()
                return True, "inserted"
        
    except Exception as e:
        st.error(f"❌ Error saving to database: {str(e)}")
        return False, None

def show_existing_data():
    """Display existing records from Product_data table"""
    try:
        engine = get_sql_connection()
        if not engine:
            return None
        
        query = f"""
            SELECT lot_no, product_name, selling_price, mapping_sku, shelf_info, product_qty
            FROM {SCHEMA}.Product_data 
            ORDER BY lot_no
        """
        df = pd.read_sql(query, engine)
        return df
        
    except Exception as e:
        st.error(f"Error fetching existing data: {str(e)}")
        return None

def check_table_exists(engine):
    """Check if Product_data table exists"""
    try:
        query = text(f"""
            SELECT COUNT(*) as count 
            FROM sys.tables 
            WHERE name = 'Product_data' AND schema_id = SCHEMA_ID('{SCHEMA}')
        """)
        with engine.connect() as conn:
            result = conn.execute(query)
            count = result.fetchone()[0]
            return count > 0
    except:
        return False

# Streamlit UI
st.set_page_config(page_title="Product Registration", layout="wide")
# Add custom CSS for better mobile experience and reduced fonts

st.markdown("""
<style>
    /* Reduce overall font sizes for mobile */
    .stApp {
        font-size: 14px !important;
    }
    
    /* Reduce title size */
    h1 {
        font-size: 1.5rem !important;
        margin-bottom: 0.5rem !important;
    }
    
    /* Reduce subheader size */
    h2, .stSubheader {
        font-size: 1.2rem !important;
    }
    
    h3 {
        font-size: 1.1rem !important;
    }
    
    /* Reduce metric labels and values */
    [data-testid="stMetricLabel"] {
        font-size: 0.8rem !important;
    }
    
    [data-testid="stMetricValue"] {
        font-size: 1.3rem !important;
    }
    
    /* Reduce button text size */
    .stButton button {
        font-size: 14px !important;
        padding: 8px 12px !important;
    }
    
    /* Reduce input label size */
    .stTextInput label, .stForm label {
        font-size: 13px !important;
    }
    
    /* Reduce info/warning/success message text */
    .stAlert {
        font-size: 13px !important;
        padding: 8px !important;
    }
    
    /* Reduce dataframe font size */
    .stDataFrame {
        font-size: 12px !important;
    }
    
    /* Reduce tabs font size */
    .stTabs [data-baseweb="tab-list"] button [data-testid="stMarkdownContainer"] p {
        font-size: 13px !important;
    }
    
    /* Reduce caption text */
    .caption, stCaption {
        font-size: 11px !important;
    }
    
    /* Make containers more compact */
    .stContainer {
        padding: 0.5rem !important;
    }
    
    /* Reduce spacing between elements */
    .element-container {
        margin-bottom: 0.5rem !important;
    }
    
    /* Make columns more compact on mobile */
    @media (max-width: 768px) {
        .stColumn {
            padding: 0 5px !important;
        }
        
        /* Hide empty columns on mobile */
        .stColumn:empty {
            display: none;
        }
        
        /* Reduce metric container padding */
        [data-testid="stMetric"] {
            padding: 8px !important;
        }
    }
    
    /* Make camera input more compact */
    [data-testid="stCameraInput"] {
        margin-bottom: 0.5rem !important;
    }
    
    /* Reduce form spacing */
    .stForm {
        gap: 0.5rem !important;
    }
    
    /* Make download button smaller */
    .stDownloadButton button {
        font-size: 13px !important;
        padding: 6px 10px !important;
    }
</style>
""", unsafe_allow_html=True)

st.title("📦 Product Registration with Barcode Scanner")
st.markdown("---")

# Initialize session state
if 'scanned_data' not in st.session_state:
    st.session_state.scanned_data = None
if 'scanned_lot' not in st.session_state:
    st.session_state.scanned_lot = None
if 'auto_lookup' not in st.session_state:
    st.session_state.auto_lookup = False
if 'existing_record_data' not in st.session_state:
    st.session_state.existing_record_data = None

# Check database connection and table existence
engine = get_sql_connection()
if engine:
    table_exists = check_table_exists(engine)
    if not table_exists:
        st.error(f"❌ Table {SCHEMA}.Product_data does not exist in the database!")
        st.stop()

# Connect to Odoo
uid, models = get_odoo_connection()

if uid and models:
    # Create tabs for different input methods
    tab1, tab2 = st.tabs(["📷 Camera Scanner", "⌨️ Manual Entry"])
    
    with tab1:
        barcode_image = st.camera_input("Position the barcode in frame", key="mobile_scanner")
        
        if barcode_image:
            import pyrxing
            from PIL import Image, ImageEnhance, ImageFilter
            import cv2
            import numpy as np
            
            # Open image
            image = Image.open(barcode_image)
            
            # Convert to RGB if needed
            if image.mode != 'RGB':
                image = image.convert('RGB')
            
            # Try multiple preprocessing techniques
            barcode_found = False
            scanned_value = None
            
            # Method 1: Original image
            result = pyrxing.read_barcode(image)
            if result:
                scanned_value = result.text
                barcode_found = True
            
            # Method 2: Convert to grayscale and back to RGB
            if not barcode_found:
                gray = image.convert('L')
                gray_rgb = gray.convert('RGB')
                result = pyrxing.read_barcode(gray_rgb)
                if result:
                    scanned_value = result.text
                    barcode_found = True
            
            # Method 3: Increase contrast
            if not barcode_found:
                enhancer = ImageEnhance.Contrast(image)
                high_contrast = enhancer.enhance(2.5)
                result = pyrxing.read_barcode(high_contrast)
                if result:
                    scanned_value = result.text
                    barcode_found = True
            
            # Method 4: Sharpen image
            if not barcode_found:
                sharpened = image.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
                result = pyrxing.read_barcode(sharpened)
                if result:
                    scanned_value = result.text
                    barcode_found = True
            
            # Method 5: Resize (make larger)
            if not barcode_found:
                width, height = image.size
                enlarged = image.resize((width*3, height*3), Image.Resampling.LANCZOS)
                result = pyrxing.read_barcode(enlarged)
                if result:
                    scanned_value = result.text
                    barcode_found = True
            
            # Method 6: Denoise
            if not barcode_found:
                img_array = np.array(image)
                denoised = cv2.fastNlMeansDenoisingColored(img_array, None, 10, 10, 7, 21)
                denoised_pil = Image.fromarray(denoised)
                result = pyrxing.read_barcode(denoised_pil)
                if result:
                    scanned_value = result.text
                    barcode_found = True
            
            if barcode_found:
                if st.session_state.get('scanned_lot') != scanned_value:
                    st.session_state.mapping_sku_input = ""
                    st.session_state.shelf_info_input = ""
                    st.session_state.existing_record_data = None
                    
                with st.spinner("Fetching product details..."):
                    product_data = get_product_by_lot(scanned_value.strip(), uid, models)
                    
                    if product_data:
                        # Check if record exists in Fabric table
                        if engine:
                            existing_record = get_existing_record(engine, scanned_value.strip())
                            if existing_record:
                                st.session_state.existing_record_data = existing_record
                                st.warning(f"⚠️ Lot number {scanned_value} already exists in database. The record will be UPDATED with new information.")
                                # Pre-fill the form with existing data
                                st.session_state.mapping_sku_input = existing_record.get('mapping_sku', '')
                                st.session_state.shelf_info_input = existing_record.get('shelf_info', '')
                        
                        st.session_state.scanned_data = product_data
                        st.session_state.scanned_lot = scanned_value
                        st.success(f"✅ Product found: {product_data['product_name']}")
                    else:
                        st.error("❌ No product found for this barcode")
                        st.session_state.scanned_data = None
            else:
                st.error("❌ Could not read barcode. Please try:")
                st.markdown("• Hold the phone steady for 2 seconds")
                st.markdown("• Ensure good lighting")
                st.markdown("• Place barcode flat and straight")
                
    with tab2:
        st.subheader("⌨️ Lot Number Entry")
        
        # Barcode/Lot number input
        lot_input = st.text_input(
            "Enter Lot Number:",
            key="manual_lot_input",
            placeholder="Enter lot number..."
        )
        # st.markdown("""
        # <script>
        #     document.querySelector('input[aria-label="Enter Lot Number:"]')?.focus();
        # </script>
        # """, unsafe_allow_html=True)
        # Lookup button
        if st.button("🔍 Lookup Product", type="primary", use_container_width=True):
            if lot_input:
                if st.session_state.get('scanned_lot') != lot_input:
                    st.session_state.mapping_sku_input = ""
                    st.session_state.shelf_info_input = ""
                    st.session_state.existing_record_data = None
                    
                with st.spinner("Fetching product details from Odoo..."):
                    product_data = get_product_by_lot(lot_input.strip(), uid, models)
                    
                    if product_data:
                        # Check if record exists in Fabric table
                        if engine:
                            existing_record = get_existing_record(engine, lot_input.strip())
                            if existing_record:
                                st.session_state.existing_record_data = existing_record
                                # st.warning(f"⚠️ Lot number {lot_input} already exists in database. The record will be UPDATED with new information.")
                                # Pre-fill the form with existing data
                                st.session_state.mapping_sku_input = existing_record.get('mapping_sku', '')
                                st.session_state.shelf_info_input = existing_record.get('shelf_info', '')
                        
                        st.session_state.scanned_data = product_data
                        st.session_state.scanned_lot = lot_input
                        st.success(f"✅ Product found: {product_data['product_name']}")
                        # if product_data.get('product_qty', 0) > 0:
                        #     st.info(f"📦 Available Quantity: {product_data['product_qty']:.2f} units")
                    else:
                        st.error("❌ No product found for this lot number")
                        st.session_state.scanned_data = None
            else:
                st.warning("⚠️ Please enter a lot number")
    
    # Product Details and Form (Common for both tabs)
    st.markdown("---")
    st.subheader("📝 Product Details")
    
    # Display fetched product information
    if st.session_state.scanned_data:
        product = st.session_state.scanned_data
        
        # Display product info in a nice container
        with st.container():
            col_info1, col_info2, col_info3, col_info4 = st.columns(4)
            with col_info1:
                st.metric("🏷️ Lot Number", product['lot_no'])
            with col_info2:
                st.metric("📦 Product Name", product['product_name'][:30] + "..." if len(product['product_name']) > 30 else product['product_name'])
            with col_info3:
                st.metric("💰 Selling Price", f"₹{product['selling_price']:,.2f}")
            # with col_info4:
            #     st.metric("📊 Available Quantity", f"{product['product_qty']:.2f} units" if product['product_qty'] > 0 else "Out of Stock")
        
        # Show existing record info if updating
        if st.session_state.existing_record_data:
            st.info(f"📝 **Updating existing record** - Current Mapping SKU: {st.session_state.existing_record_data.get('mapping_sku', 'N/A')}")
        
        # Form for additional data
        with st.form(key="product_registration_form"):
            st.subheader("➕ Additional Information")
            
            mapping_sku = st.text_input(
                "📌 Mapping SKU *:",
                placeholder="Enter mapping SKU...",
                help="SKU mapping for internal reference (Required)",
                key="mapping_sku_input",
                # value=st.session_state.get('mapping_sku_input', '')
            )
            
            shelf_info = st.text_input(
                "📍 Shelf Information (Optional):",
                placeholder="Enter shelf/rack location...",
                help="Physical location of the product - not mandatory",
                key="shelf_info_input",
                # value=st.session_state.get('shelf_info_input', '')
            )
            
            # Submit button
            submit_label = "🔄 Update Database" if st.session_state.existing_record_data else "💾 Save to Database"
            submitted = st.form_submit_button(submit_label, type="primary", use_container_width=True)
            
            if submitted:
                if mapping_sku:
                    # Prepare data for saving
                    save_data = {
                        'lot_no': product['lot_no'],
                        'product_name': product['product_name'],
                        'selling_price': product['selling_price'],
                        'mapping_sku': mapping_sku,
                        'shelf_info': shelf_info if shelf_info else '',
                        'product_qty': product['product_qty']
                    }
                    
                    with st.spinner("Saving to database..."):
                        success, action = save_to_fabric_table(save_data)
                        if success:
                            if action == "updated":
                                st.success("✅ Product data updated successfully!")
                            else:
                                st.success("✅ Product data saved successfully!")
                            # Clear the scanned data after successful save
                            st.session_state.scanned_data = None
                            st.session_state.scanned_lot = None
                            st.session_state.existing_record_data = None
                            st.rerun()
                        else:
                            st.error("❌ Failed to save data")
                else:
                    st.warning("⚠️ Please fill Mapping SKU field (Required)")
    else:
        st.info("👆 Scan a barcode using camera or enter lot number manually to see product details")
    
    # Display existing records
    st.markdown("---")
    st.subheader("📊 Existing Product Records")
    
    # Refresh button
    col_refresh, col_empty = st.columns([1, 4])
    with col_refresh:
        if st.button("🔄 Refresh Data", use_container_width=True):
            st.rerun()
    
    # Show data table
    df = show_existing_data()
    if df is not None and not df.empty:
        if 'product_qty' in df.columns:
            df = df.drop(columns=['product_qty'])
        st.dataframe(
            df,
            use_container_width=True,
            column_config={
                "lot_no": "🏷️ Lot Number",
                "product_name": "📦 Product Name",
                "selling_price": st.column_config.NumberColumn("💰 Selling Price (₹)", format="₹%d"),
                "mapping_sku": "📌 Mapping SKU",
                "shelf_info": "📍 Shelf Info"
                # "product_qty": st.column_config.NumberColumn("📊 Quantity", format="%.2f")
            },
            hide_index=True
        )
        
        # Download option
        csv = df.to_csv(index=False)
        st.download_button(
            label="📥 Download Data as CSV",
            data=csv,
            file_name="product_data_export.csv",
            mime="text/csv",
            use_container_width=True
        )
        
        # Show record count
        st.info(f"📊 Total records: {len(df)}")
    else:
        st.info("No records found in the database. Scan and save your first product!")

else:
    st.error("Failed to connect to Odoo. Please check your credentials.")

# Footer
st.markdown("---")
st.markdown("### 📋 Instructions:")
st.markdown("""
1. **Camera Scanner Tab**: Point your phone camera at the barcode - it will scan automatically!
2. **Manual Entry Tab**: Type lot number if camera doesn't work
3. Product details will auto-fetch from Odoo
4. Enter **Mapping SKU** (required) and **Shelf Info** (optional)
5. Click **Save to Database** to store the information
6. **If a lot number already exists**, the system will update the existing record instead of creating a duplicate
""")
