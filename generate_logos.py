"""
Generates simple SVG placeholder logos for each studio.
Run once — creates PNG files in static/logos/
In production, studios upload their real logo images.
"""
import os

os.makedirs("static/logos", exist_ok=True)

logos = [
    {
        "filename": "shinepro.png",
        "text": "SHINE PRO",
        "sub": "DETAILING",
        "bg": "#0A0A0A",
        "color": "#C8A96E",
    },
    {
        "filename": "apex.png",
        "text": "APEX",
        "sub": "DETAIL STUDIO",
        "bg": "#0A0A0A",
        "color": "#E8E8E8",
    },
    {
        "filename": "velvet.png",
        "text": "VELVET",
        "sub": "AUTO SPA",
        "bg": "#0A0A0A",
        "color": "#A78BFA",
    },
]

# We'll save as SVG (browsers render SVG in <img> tags fine)
# Rename .png → .svg in the DB if you prefer, but .png filename with SVG content works too
for logo in logos:
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="200" height="60" viewBox="0 0 200 60">
  <rect width="200" height="60" fill="{logo['bg']}"/>
  <text x="10" y="34" font-family="Georgia, serif" font-size="22" font-weight="bold"
        fill="{logo['color']}" letter-spacing="3">{logo['text']}</text>
  <text x="11" y="50" font-family="Georgia, serif" font-size="10"
        fill="{logo['color']}" opacity="0.5" letter-spacing="2">{logo['sub']}</text>
</svg>"""
    # Save as .svg with the .png filename — Flask serves it fine as static
    path = f"static/logos/{logo['filename'].replace('.png', '.svg')}"
    with open(path, "w") as f:
        f.write(svg)
    print(f"Created: {path}")

print("\nLogos generated in static/logos/")
print("Update the logo filenames in seed.py to .svg if needed,")
print("or replace with real PNG files later.")
