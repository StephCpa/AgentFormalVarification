from pathlib import Path
import importlib.util, subprocess, json
ROOT=Path(__file__).resolve().parent
skill=Path(r'C:\Users\Administrator\.codex\plugins\cache\openai-primary-runtime\documents\26.1017.22136\skills\documents\render_docx.py')
spec=importlib.util.spec_from_file_location('doc_renderer',skill)
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
doc=ROOT.parent/'AVCL形式化验证语言与执行系统详细设计_v0.1.docx'
def wps_pdf(doc_path, user_profile, convert_tmp_dir, stem, verbose=False):
    pdf=Path(convert_tmp_dir)/(stem+'.pdf')
    r=subprocess.run([r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'export_wps.ps1'),'-InputDoc',str(doc_path),'-OutputPdf',str(pdf)],capture_output=True,text=True,timeout=120)
    if r.returncode or not pdf.exists():raise RuntimeError(r.stdout+r.stderr)
    return str(pdf), 'WPS COM export used because no bundled LibreOffice is available on Windows.'
mod.convert_to_pdf=wps_pdf
pages=mod.rasterize(str(doc),str(ROOT/'render'),120,verbose=True,emit_pdf=True)
print(json.dumps({'pages':len(pages),'renderer':'packaged render_docx.py rasterization with WPS PDF export'},ensure_ascii=False))
