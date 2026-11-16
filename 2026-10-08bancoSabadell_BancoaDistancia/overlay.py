from io import BytesIO
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

#source = "/storage/emulated/0/Download/form.pdf"
#output = "/storage/emulated/0/Download/filled.pdf"
source = "form.pdf"
output = "filled.pdf"

reader = PdfReader(source)
page = reader.pages[0]  # First page
width = float(page.mediabox.width)
height = float(page.mediabox.height)

buffer = BytesIO()
c = canvas.Canvas(buffer, pagesize=(width, height))
c.setFont("Helvetica", 11)

# Coordinates are points from the bottom-left of the page.
c.drawString(100, height - 150, "Jane Doe")
c.drawString(100, height - 190, "jane@example.com")

c.save()
buffer.seek(0)

overlay = PdfReader(buffer).pages[0]
page.merge_page(overlay)

writer = PdfWriter()
writer.append(reader)
with open(output, "wb") as f:
    writer.write(f)

print("Saved:", output)
