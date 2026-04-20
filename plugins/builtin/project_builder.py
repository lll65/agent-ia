"""
Plugin de génération de projet complet avec IA.
Groq génère tous les fichiers → preview HTML → ZIP téléchargeable.
"""
import json
import re
import zipfile
from pathlib import Path

from plugins.base import Plugin
from config import config


_PROJECT_PROMPT = """Tu es un développeur expert. Génère un projet {project_type} COMPLET et FONCTIONNEL pour: {description}

Réponds UNIQUEMENT en JSON valide (sans markdown):
{{
  "name": "nom-du-projet-en-kebab-case",
  "description": "description courte",
  "main_file": "index.html",
  "files": {{
    "index.html": "<!DOCTYPE html>...contenu HTML complet avec CSS et JS inline...",
    "README.md": "# nom\\n\\ndescription...instructions..."
  }}
}}

RÈGLES STRICTES:
- Génère du VRAI code fonctionnel et complet dans chaque fichier
- Pour projets web: Bootstrap 5 CDN, CSS moderne, JS vanilla fonctionnel
- Pour projets Python: code complet avec if __name__=='__main__'
- Le fichier principal doit être beau et professionnel
- Maximum 8 fichiers
- Pas de placeholder "// TODO" — tout doit marcher"""

_PREVIEW_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Preview — {name}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, sans-serif; background: #0f0f1a; color: #e2e2f0; }}
  .header {{ background: #1a1a2e; padding: 1rem 1.5rem; border-bottom: 2px solid #6366f1; display: flex; align-items: center; gap: 1rem; }}
  .header h1 {{ font-size: 1.1rem; color: #a5b4fc; }}
  .badge {{ background: #6366f1; color: white; padding: .2rem .6rem; border-radius: 99px; font-size: .75rem; }}
  .layout {{ display: flex; height: calc(100vh - 56px); }}
  .sidebar {{ width: 220px; background: #12121f; border-right: 1px solid #2a2a4a; padding: 1rem; overflow-y: auto; flex-shrink: 0; }}
  .sidebar h2 {{ font-size: .8rem; color: #6b7280; text-transform: uppercase; letter-spacing: .05em; margin-bottom: .75rem; }}
  .file-item {{ padding: .4rem .6rem; border-radius: 6px; cursor: pointer; font-size: .85rem; margin-bottom: 2px; color: #c4c4e0; transition: background .15s; }}
  .file-item:hover {{ background: #1e1e3a; }}
  .file-item.active {{ background: #2d2d5a; color: #a5b4fc; }}
  .file-icon {{ margin-right: .4rem; }}
  .main {{ flex: 1; display: flex; flex-direction: column; overflow: hidden; }}
  .preview-area {{ flex: 1; border: none; background: white; }}
  .code-area {{ flex: 1; background: #1e1e2e; padding: 1rem; overflow: auto; font-family: 'Courier New', monospace; font-size: .82rem; line-height: 1.5; white-space: pre; color: #cdd6f4; display: none; }}
  .tab-bar {{ background: #1a1a2e; border-bottom: 1px solid #2a2a4a; display: flex; gap: 0; }}
  .tab {{ padding: .5rem 1rem; font-size: .85rem; cursor: pointer; color: #6b7280; border-bottom: 2px solid transparent; }}
  .tab.active {{ color: #a5b4fc; border-bottom-color: #6366f1; }}
  .stats {{ display: flex; gap: 1rem; padding: .5rem 1.5rem; background: #12121f; border-bottom: 1px solid #2a2a4a; font-size: .78rem; color: #6b7280; }}
</style>
</head>
<body>
<div class="header">
  <span>🗂️</span>
  <h1>{name}</h1>
  <span class="badge">{file_count} fichiers</span>
  <span class="badge" style="background:#10b981">{project_type}</span>
</div>
<div class="stats">
  <span>📁 {name}/</span>
  <span>💾 {total_size}</span>
  <span>🤖 Généré par MasterAgent-Gros</span>
</div>
<div class="layout">
  <div class="sidebar">
    <h2>Fichiers</h2>
    {file_list_html}
  </div>
  <div class="main">
    <div class="tab-bar">
      <div class="tab active" onclick="showPreview()" id="tab-preview">👁️ Preview</div>
      <div class="tab" onclick="showCode()" id="tab-code">💻 Code</div>
    </div>
    <iframe class="preview-area" id="preview-frame" src="{main_file}" sandbox="allow-scripts allow-same-origin"></iframe>
    <div class="code-area" id="code-area"></div>
  </div>
</div>
<script>
const files = {files_json};
let activeFile = '{main_file}';

function showPreview() {{
  document.getElementById('preview-frame').style.display = 'block';
  document.getElementById('code-area').style.display = 'none';
  document.getElementById('tab-preview').classList.add('active');
  document.getElementById('tab-code').classList.remove('active');
}}
function showCode() {{
  document.getElementById('preview-frame').style.display = 'none';
  const ca = document.getElementById('code-area');
  ca.style.display = 'block';
  ca.textContent = files[activeFile] || '(contenu non disponible)';
  document.getElementById('tab-code').classList.add('active');
  document.getElementById('tab-preview').classList.remove('active');
}}
function selectFile(fname) {{
  activeFile = fname;
  document.querySelectorAll('.file-item').forEach(el => el.classList.remove('active'));
  document.getElementById('fi-' + fname.replace(/[^a-z0-9]/gi,'_')).classList.add('active');
  const ca = document.getElementById('code-area');
  if (ca.style.display !== 'none') ca.textContent = files[fname] || '';
  const pf = document.getElementById('preview-frame');
  if (pf.style.display !== 'none' && (fname.endsWith('.html') || fname.endsWith('.htm'))) {{
    pf.src = fname;
  }}
}}
</script>
</body>
</html>"""


def _file_icon(name: str) -> str:
    ext = Path(name).suffix.lower()
    return {"html": "🌐", ".css": "🎨", ".js": "⚡", ".py": "🐍",
            ".md": "📝", ".txt": "📄", ".json": "📋", ".yml": "⚙️",
            ".yaml": "⚙️", ".sh": "🔧", ".dockerfile": "🐳",
            "dockerfile": "🐳", ".sql": "🗄️"}.get(ext, "📄")


class FullProjectPlugin(Plugin):
    name = "build_full_project"
    description = "Génère un projet complet (code réel + preview HTML + ZIP téléchargeable) depuis une description."
    parameters = {
        "description": {"type": "string", "description": "Description détaillée du projet à créer", "required": True},
        "project_type": {"type": "string", "description": "Type: web, python-api, react, game, dashboard, cli", "required": False},
    }

    def run(self, description: str, project_type: str = "web") -> str:
        from llm.client import chat

        # 1. Génère tous les fichiers via Groq
        try:
            raw = chat([
                {"role": "system", "content": "Tu es un développeur expert. Tu génères du code complet et fonctionnel. JSON pur uniquement."},
                {"role": "user", "content": _PROJECT_PROMPT.format(
                    description=description, project_type=project_type)},
            ], temperature=0.3)

            m = re.search(r'\{[\s\S]+\}', raw)
            if not m:
                return "❌ Impossible de parser le plan de projet."
            plan = json.loads(m.group())
        except Exception as e:
            return f"❌ LLM error: {e}"

        proj_name = re.sub(r'[^\w-]', '-', plan.get("name", "projet").strip())[:40]
        files = plan.get("files", {})
        main_file = plan.get("main_file", next(iter(files), "index.html"))
        out_dir = Path(config.OUTPUT_DIR) / "projects" / proj_name
        out_dir.mkdir(parents=True, exist_ok=True)

        # 2. Sauvegarde tous les fichiers
        saved = []
        total_bytes = 0
        for fname, content in files.items():
            fpath = out_dir / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            text = str(content)
            fpath.write_text(text, encoding="utf-8")
            saved.append(fname)
            total_bytes += len(text.encode())

        if not saved:
            return "❌ Aucun fichier généré."

        # 3. Génère preview.html
        file_list_html = "\n".join(
            f'<div class="file-item" id="fi_{fn.replace(".", "_").replace("/", "_")}" '
            f'onclick="selectFile(\'{fn}\')">'
            f'<span class="file-icon">{_file_icon(fn)}</span>{fn}</div>'
            for fn in saved
        )
        files_json_obj = {}
        for fn in saved:
            try:
                files_json_obj[fn] = (out_dir / fn).read_text(encoding="utf-8")
            except Exception:
                files_json_obj[fn] = ""

        size_str = f"{total_bytes / 1024:.1f} KB" if total_bytes > 1024 else f"{total_bytes} B"
        preview_html = _PREVIEW_TEMPLATE.format(
            name=proj_name,
            file_count=len(saved),
            project_type=project_type,
            total_size=size_str,
            file_list_html=file_list_html,
            main_file=main_file,
            files_json=json.dumps(files_json_obj, ensure_ascii=False),
        )
        preview_path = out_dir / "_preview.html"
        preview_path.write_text(preview_html, encoding="utf-8")

        # 4. Crée le ZIP
        zip_path = Path(config.OUTPUT_DIR) / "projects" / f"{proj_name}.zip"
        with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
            for fn in saved:
                zf.write(str(out_dir / fn), f"{proj_name}/{fn}")

        return (
            f"✅ Projet '{proj_name}' généré!\n"
            f"Fichiers: {', '.join(saved)}\n"
            f"ZIP: {zip_path}\n"
            f"Preview: {preview_path}"
        )
