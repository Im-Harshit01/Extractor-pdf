import os
import csv
import re
import pdfplumber

PDF_FOLDER = "client_receipts"
OUTPUT_CSV = "monthly_expense_report.csv"

if not os.path.exists(PDF_FOLDER):
    os.makedirs(PDF_FOLDER)

# Define column layout for the final output spreadsheet
fields = ['File Name', 'Vendor/Bill Name', 'All Dates Found', 'Total Amount']

print("🚀 Running precision-anchored invoice extractor...")

with open(OUTPUT_CSV, mode='w', newline='', encoding='utf-8') as csv_file:
    writer = csv.writer(csv_file)
    writer.writerow(fields)

    for file_name in os.listdir(PDF_FOLDER):
        if file_name.endswith('.pdf'):
            file_path = os.path.join(PDF_FOLDER, file_name)
            
            full_text = ""
            try:
                # Open with structural layout parameter enabled to maintain tabular isolation
                with pdfplumber.open(file_path) as pdf:
                    for page in pdf.pages:
                        text_content = page.extract_text(layout=True)
                        if text_content:
                            full_text += text_content + "\n"
            except Exception as e:
                print(f"❌ Error parsing {file_name}: {e}")
                continue

            if not full_text.strip():
                writer.writerow([file_name, "Scanned Document / Empty", "Not Found", "Not Found"])
                continue

            # =========================================================================
            # 1. EXTRACT VENDOR NAME
            # =========================================================================
            lines = [line.strip() for line in full_text.split('\n') if line.strip()]
            vendor_name = lines[0] if lines else "Unknown Vendor"
            if len(vendor_name) > 40:
                vendor_name = vendor_name[:40] + "..."

            # =========================================================================
            # 2. EXTRACT ALL DATES
            # =========================================================================
            found_dates = re.findall(r'\d{1,4}[/-][\w]{2,3}[/-]\d{2,4}|\d{1,4}[/-]\d{1,2}[/-]\d{1,4}', full_text)
            unique_dates = list(set(found_dates))
            date_string = ", ".join(unique_dates) if unique_dates else "Not Found"

            # =========================================================================
            # 3. CONTEXT-ANCHORED TOTAL ENGINE
            # =========================================================================
            total_detected = "Not Found"
            
            # High-priority specific transactional row labels
            PRIMARY_ANCHORS = ["amount payable", "net payable", "grand total", "total payable", "total due", "total paid"]
            # Generic fallback rows
            SECONDARY_ANCHORS = ["total", "payable", "due", "paid", "amount"]

            all_lines = full_text.split('\n')

            #Phase 1: High-Precision Search (Bottom to Top)
            for idx, line in enumerate(reversed(all_lines)):
                # Because we are looping in reverse, we calculate the real line position in the original list
                actual_idx = len(all_lines) - 1 - idx
                line_lower = line.lower()
                
                if any(phrase in line_lower for phrase in PRIMARY_ANCHORS):
                    # 1. First, check if the price is on the EXACT same line
                    prices = re.findall(r'(?:₹?\s*)(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)', line)
                    
                    valid_line_prices = []
                    for p in prices:
                        clean_p = p.replace(',', '').strip()
                        try:
                            val = float(clean_p)
                            if val >= 10.0:
                                valid_line_prices.append(clean_p)
                        except ValueError:
                            continue
                            
                    if valid_line_prices:
                        total_detected = valid_line_prices[-1]
                        break
                    
                    # 2. 💡 MULTI-LINE WINDOW FIX: If no number on this line, check the next lines directly below it
                    # (This looks forward in the original document layout order)
                    window_found = False
                    for next_offset in range(1, 6):  # Check up to 5 lines below
                        if actual_idx + next_offset < len(all_lines):
                            next_line = all_lines[actual_idx + next_offset]
                            # Find any valid price numbers on the following line
                            next_prices = re.findall(r'(?:₹?\s*)(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)', next_line)
                            
                            valid_next_prices = []
                            for p in next_prices:
                                clean_p = p.replace(',', '').strip()
                                try:
                                    val = float(clean_p)
                                    if val >= 10.0:
                                        valid_next_prices.append(clean_p)
                                except ValueError:
                                    continue
                                    
                            if valid_next_prices:
                                total_detected = valid_next_prices[-1]
                                window_found = True
                                break
                    if window_found:
                        break

            # Phase 2: Generic Row Extraction with Distraction Filters
            if total_detected == "Not Found":
                for line in reversed(all_lines):
                    line_lower = line.lower()
                    if any(word in line_lower for word in SECONDARY_ANCHORS):
                        # Drop lines that commonly reference past balances or quantity details
                        if any(neg in line_lower for neg in ["previous", "balance", "tax share", "qty", "quantity", "savings"]):
                            continue
                            
                        prices = re.findall(r'(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)', line)
                        valid_line_prices = []
                        for p in prices:
                            clean_p = p.replace(',', '').strip()
                            try:
                                val = float(clean_p)
                                if val >= 10.0:
                                    valid_line_prices.append(clean_p)
                            except ValueError:
                                continue
                        if valid_line_prices:
                            total_detected = valid_line_prices[-1]
                            break

            # Phase 3: Global Structural Fallback
            if total_detected == "Not Found":
                matches = re.findall(r'(?:grand total|net payable|amount payable|total due|total)[^\d\n]*(\d{2,6}(?:\.\d{1,2})?)', full_text.lower())
                if matches:
                    total_detected = matches[-1].replace(',', '').strip()

            writer.writerow([file_name, vendor_name, date_string, total_detected])
            print(f"✅ Extracted: {file_name} -> Total: {total_detected}")

print(f"\n🎉 Script run complete. Open '{OUTPUT_CSV}' in VS Code to see your flawless data logs.")
