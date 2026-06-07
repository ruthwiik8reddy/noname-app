import os
from .config import BASE_DIR

os.makedirs(os.path.join(BASE_DIR, "static", "logos"), exist_ok=True)

logos = [
    {
        "filename": "shinepro.svg",
        "text": "SHINE PRO",
        "sub": "DETAILING",
        "bg": "#0A0A0A",
        "color": "#C8A96E",
    },
    {
        "filename": "apex.svg",
        "text": "APEX",
        "sub": "DETAIL STUDIO",
        "bg": "#0A0A0A",
        "color": "#E8E8E8",
    },
    {
        "filename": "velvet.svg",
        "text": "VELVET",
        "sub": "AUTO SPA",
        "bg": "#0A0A0A",
        "color": "#A78BFA",
    },
]

for logo in logos:
    svg = f"""<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"200\" height=\"60\" viewBox=\"0 0 200 60\">\n  <rect width=\"200\" height=\"60\" fill=\"{logo['bg']}\"/>\n  <text x=\"10\" y=\"34\" font-family=\"Georgia, serif\" font-size=\"22\" font-weight=\"bold\"\n        fill=\"{logo['color']}\" letter-spacing=\"3\">{logo['text']}</text>\n  <text x=\"11\" y=\"50\" font-family=\"Georgia, serif\" font-size=\"10\"\n        fill=\"{logo['color']}\" opacity=\"0.5\" letter-spacing=\"2\">{logo['sub']}</text>\n</svg>"""
    path = os.path.join(BASE_DIR, "static", "logos", logo["filename"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"Created: {path}")

print("\nLogos generated in static/logos/")
print("Update the logo filenames in seed.py to .svg if needed, or replace with real PNG files later.")
