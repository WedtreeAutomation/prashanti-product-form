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
import asyncio
from threading import Thread

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

# Real-time Barcode Scanner Class
class RealtimeBarcodeScanner(VideoTransformerBase):
    def __init__(self):
        self.barcode_data = None
        self.last_scanned = None
        self.scan_interval = 5  # Minimum seconds between scans of same barcode
        self.last_scan_time = 0
        import time
        self.time = time
        
    def recv(self, frame):
        # Get current time
        current_time = self.time.time()
        
        # Convert frame to image
        img = frame.to_ndarray(format="bgr24")
        
        # Convert to PIL Image for pyrxing
        pil_image = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        
        # Try to detect barcode
        result = pyrxing.read_barcode(pil_image)
        
        if result and result.text:
            barcode_value = result.text
            current_time = self.time.time()
            
            # Check if it's a new barcode or enough time has passed
            if (self.last_scanned != barcode_value or 
                current_time - self.last_scan_time > self.scan_interval):
                self.barcode_data = barcode_value
                self.last_scanned = barcode_value
                self.last_scan_time = current_time
        
        # Draw barcode data on frame if available
        if self.barcode_data:
            cv2.putText(img, f"Scanned: {self.barcode_data}", 
                       (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 
                       1, (0, 255, 0), 2)
            cv2.putText(img, "Press 'Save' to confirm", 
                       (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 
                       0.7, (255, 255, 0), 2)
        
        return img

# Alternative using OpenCV directly for faster processing
class FastBarcodeScanner(VideoTransformerBase):
    def __init__(self):
        self.barcode_data = None
        self.last_scanned = None
        self.scan_interval = 5
        self.last_scan_time = 0
        self.processing = False
        import time
        self.time = time
        
    def recv(self, frame):
        current_time = self.time.time()
        img = frame.to_ndarray(format="bgr24")
        
        # Process every few frames to maintain performance
        if not self.processing:
            self.processing = True
            
            try:
                # Convert to grayscale for better barcode detection
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                
                # Try multiple barcode detection methods
                barcode_value = None
                
                # Method 1: Try detecting with pyzbar if available
                try:
                    from pyzbar.pyzbar import decode
                    decoded_objects = decode(gray)
                    if decoded_objects:
                        barcode_value = decoded_objects[0].data.decode('utf-8')
                except:
                    pass
                
                # Method 2: Use pyrxing as fallback
                if not barcode_value:
                    pil_image = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
                    result = pyrxing.read_barcode(pil_image)
                    if result and result.text:
                        barcode_value = result.text
                
                if barcode_value and (self.last_scanned != barcode_value or 
                                      current_time - self.last_scan_time > self.scan_interval):
                    self.barcode_data = barcode_value
                    self.last_scanned = barcode_value
                    self.last_scan_time = current_time
                    
            finally:
                self.processing = False
        
        # Display on frame
        if self.barcode_data:
            cv2.putText(img, f"✓ {self.barcode_data}", (10, 30), 
                       cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
            
            # Draw rectangle around scan area (center of frame)
            h, w = img.shape[:2]
            center_x, center_y = w // 2, h // 2
            rect_size = min(w, h) // 3
            cv2.rectangle(img, 
                         (center_x - rect_size, center_y - rect_size),
                         (center_x + rect_size, center_y + rect_size),
                         (0, 255, 0), 2)
        
        return img

# ... (keep all your existing functions: get_odoo_connection, get_product_by_lot, 
#      get_sql_connection, check_record_exists, get_existing_record, 
#      save_to_fabric_table, show_existing_data, check_table_exists)

# Updated Streamlit UI with real-time scanning
st.set_page_config(page_title="Product Registration", layout="wide")

# Custom CSS for better mobile experience
st.markdown("""
<style>
    /* Your existing CSS styles */
    .stApp {
        font-size: 14px !important;
    }
    h1 {
        font-size: 1.5rem !important;
        margin-bottom: 0.5rem !important;
    }
    /* Add style for real-time scanner */
    .scanner-container {
        position: relative;
        margin-bottom: 1rem;
    }
    .scanner-overlay {
        position: absolute;
        top: 50%;
        left: 50%;
        transform: translate(-50%, -50%);
        border: 2px solid #00ff00;
        width: 70%;
        height: 40%;
        pointer-events: none;
        z-index: 1;
    }
    @media (max-width: 768px) {
        [data-testid="stImage"] {
            max-height: 400px;
            overflow: hidden;
        }
    }
</style>
""", unsafe_allow_html=True)

st.title("📦 Product Registration with Real-time Scanner")
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
if 'scanner_active' not in st.session_state:
    st.session_state.scanner_active = False
if 'pending_barcode' not in st.session_state:
    st.session_state.pending_barcode = None

# Check database connection
engine = get_sql_connection()
if engine:
    table_exists = check_table_exists(engine)
    if not table_exists:
        st.error(f"❌ Table {SCHEMA}.Product_data does not exist in the database!")
        st.stop()

# Connect to Odoo
uid, models = get_odoo_connection()

if uid and models:
    # Create tabs
    tab1, tab2, tab3 = st.tabs(["📷 Real-time Scanner", "🖼️ Image Upload", "⌨️ Manual Entry"])
    
    with tab1:
        st.subheader("🎥 Real-time Barcode Scanner")
        st.markdown("Point your camera at the barcode - it will detect automatically!")
        
        # Create columns for controls
        col1, col2, col3 = st.columns([1, 1, 2])
        
        with col1:
            start_scanner = st.button("▶️ Start Scanner", type="primary", use_container_width=True)
        
        with col2:
            stop_scanner = st.button("⏹️ Stop Scanner", use_container_width=True)
        
        # Placeholder for barcode display
        barcode_display = st.empty()
        
        # Real-time scanner
        if start_scanner:
            st.session_state.scanner_active = True
            st.session_state.pending_barcode = None
        
        if stop_scanner:
            st.session_state.scanner_active = False
        
        if st.session_state.scanner_active:
            # Initialize the scanner
            ctx = webrtc_streamer(
                key="realtime-scanner",
                mode=WebRtcMode.SENDRECV,
                video_transformer_factory=FastBarcodeScanner,
                async_processing=True,
                media_stream_constraints={"video": True, "audio": False},
                video_processor_factory=None,
                rtc_configuration={"iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}]}
            )
            
            if ctx and ctx.video_transformer:
                # Check for scanned barcode
                barcode_value = ctx.video_transformer.barcode_data
                
                if barcode_value and barcode_value != st.session_state.get('last_displayed_barcode'):
                    st.session_state.last_displayed_barcode = barcode_value
                    barcode_display.success(f"✅ Scanned: **{barcode_value}**")
                    
                    # Auto-lookup product
                    if st.button(f"🔍 Lookup Product for {barcode_value}", key="auto_lookup_btn"):
                        with st.spinner("Fetching product details..."):
                            product_data = get_product_by_lot(barcode_value.strip(), uid, models)
                            
                            if product_data:
                                st.session_state.scanned_lot = barcode_value
                                st.session_state.scanned_data = product_data
                                st.session_state.scanner_active = False
                                st.rerun()
                            else:
                                st.error("❌ No product found for this barcode")
                else:
                    barcode_display.info("📷 Scanning... Position barcode in frame")
            else:
                barcode_display.warning("⚠️ Waiting for camera access...")
            
            # Instructions
            with st.expander("📖 Scanner Tips"):
                st.markdown("""
                - **Hold the barcode steady** in front of the camera
                - **Ensure good lighting** - avoid shadows
                - **Keep barcode flat** and parallel to camera
                - Scanner automatically detects when barcode is in frame
                - Click 'Lookup Product' button when a barcode is detected
                """)
    
    with tab2:
        st.subheader("📸 Upload Image")
        st.markdown("Upload a clear image of the barcode")
        
        barcode_image = st.file_uploader(
            "Choose barcode image", 
            type=['jpg', 'jpeg', 'png', 'bmp', 'tiff'],
            key="image_upload"
        )
        
        if barcode_image:
            # Display uploaded image
            image = Image.open(barcode_image)
            st.image(image, caption="Uploaded Image", use_column_width=True)
            
            # Process image for barcode
            if st.button("🔍 Scan Barcode from Image", type="primary", use_container_width=True):
                with st.spinner("Scanning image..."):
                    # Multiple preprocessing attempts
                    barcode_found = False
                    scanned_value = None
                    
                    # Try different methods
                    for method_name, processed_image in [
                        ("Original", image),
                        ("Grayscale", image.convert('L').convert('RGB')),
                        ("High Contrast", ImageEnhance.Contrast(image).enhance(2.5)),
                        ("Sharpened", image.filter(ImageFilter.UnsharpMask(radius=2, percent=150)))
                    ]:
                        result = pyrxing.read_barcode(processed_image)
                        if result and result.text:
                            scanned_value = result.text
                            barcode_found = True
                            break
                    
                    if barcode_found:
                        st.success(f"✅ Barcode detected: **{scanned_value}**")
                        
                        # Fetch product details
                        product_data = get_product_by_lot(scanned_value.strip(), uid, models)
                        
                        if product_data:
                            st.session_state.scanned_data = product_data
                            st.session_state.scanned_lot = scanned_value
                            st.rerun()
                        else:
                            st.error("❌ No product found for this barcode")
                    else:
                        st.error("❌ Could not read barcode from image. Please try a clearer image.")
    
    with tab3:
        st.subheader("⌨️ Manual Lot Number Entry")
        
        lot_input = st.text_input(
            "Enter Lot Number:",
            key="manual_lot_input",
            placeholder="Enter lot number manually..."
        )
        
        if st.button("🔍 Lookup Product", type="primary", use_container_width=True):
            if lot_input:
                with st.spinner("Fetching product details from Odoo..."):
                    product_data = get_product_by_lot(lot_input.strip(), uid, models)
                    
                    if product_data:
                        # Check if record exists
                        if engine:
                            existing_record = get_existing_record(engine, lot_input.strip())
                            if existing_record:
                                st.session_state.existing_record_data = existing_record
                                st.session_state.mapping_sku_input = existing_record.get('mapping_sku', '')
                                st.session_state.shelf_info_input = existing_record.get('shelf_info', '')
                        
                        st.session_state.scanned_data = product_data
                        st.session_state.scanned_lot = lot_input
                        st.success(f"✅ Product found: {product_data['product_name']}")
                    else:
                        st.error("❌ No product found for this lot number")
            else:
                st.warning("⚠️ Please enter a lot number")
    
    # Product Details and Form (Common for all tabs)
    st.markdown("---")
    st.subheader("📝 Product Details")
    
    # Display fetched product information
    if st.session_state.scanned_data:
        product = st.session_state.scanned_data
        
        # Display product info in columns
        with st.container():
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("🏷️ Lot Number", product['lot_no'])
            with col2:
                product_name_display = product['product_name'][:40] + "..." if len(product['product_name']) > 40 else product['product_name']
                st.metric("📦 Product Name", product_name_display)
            with col3:
                st.metric("💰 Selling Price", f"₹{product['selling_price']:,.2f}")
        
        # Show existing record info if updating
        if st.session_state.existing_record_data:
            st.info(f"📝 **Updating existing record** - Current Mapping SKU: {st.session_state.existing_record_data.get('mapping_sku', 'N/A')}")
        
        # Form for additional data
        with st.form(key="product_registration_form"):
            st.subheader("➕ Additional Information")
            
            mapping_sku = st.text_input(
                "📌 Mapping SKU *:",
                placeholder="Enter mapping SKU...",
                key="mapping_sku_input"
            )
            
            shelf_info = st.text_input(
                "📍 Shelf Information (Optional):",
                placeholder="Enter shelf/rack location...",
                key="shelf_info_input"
            )
            
            # Submit button
            submit_label = "🔄 Update Database" if st.session_state.existing_record_data else "💾 Save to Database"
            submitted = st.form_submit_button(submit_label, type="primary", use_container_width=True)
            
            if submitted:
                if mapping_sku:
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
                            st.success("✅ Product data saved/updated successfully!")
                            st.session_state.scanned_data = None
                            st.session_state.scanned_lot = None
                            st.session_state.existing_record_data = None
                            # Clear scanner state
                            st.session_state.scanner_active = False
                            st.rerun()
                        else:
                            st.error("❌ Failed to save data")
                else:
                    st.warning("⚠️ Please fill Mapping SKU field (Required)")
    
    # Display existing records
    st.markdown("---")
    st.subheader("📊 Existing Product Records")
    
    # Refresh button
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
        
        st.info(f"📊 Total records: {len(df)}")
    else:
        st.info("No records found in the database. Scan and save your first product!")

# Add required imports at top
from PIL import Image, ImageEnhance, ImageFilter

# Instructions
st.markdown("---")
st.markdown("### 📋 Quick Start Guide:")
st.markdown("""
1. **Real-time Scanner**: 
   - Click 'Start Scanner' and allow camera access
   - Hold barcode in front of camera - it will detect automatically!
   - Click 'Lookup Product' when barcode is detected
   
2. **Image Upload**: 
   - Upload a barcode image file
   - Click 'Scan Barcode from Image'
   
3. **Manual Entry**: 
   - Type lot number manually if needed
   
4. **Complete Registration**:
   - Review product details
   - Enter Mapping SKU (required)
   - Add Shelf Info (optional)
   - Click Save/Update
""")
