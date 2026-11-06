import io
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

import regex
from pypdf import PdfReader

MAX_UPLOAD = 10 * 1024 * 1024
MAX_TEXT = 60_000
MAX_PASSAGE = 750
WORD = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
SCRIPTS = ('Devanagari', 'Gujarati', 'Bengali', 'Gurmukhi', 'Oriya', 'Tamil', 'Telugu',
           'Kannada', 'Malayalam', 'Sinhala', 'Arabic', 'Meetei_Mayek', 'Ol_Chiki', 'Latin')
SCRIPT_PATTERN = regex.compile('|'.join(fr'(?P<{s}>\p{{Script={s}}}+)' for s in SCRIPTS) + r'|(?P<Other>\p{L}+?)|(?P<Common>.)', regex.DOTALL)
DEPENDENT = re.compile(r'^(?:but|however|he|she|they|it|then|and|लेकिन|परंतु|उसने|वह|फिर|और|પણ|પરંતુ|તેણે|તે|પછી|અને)(?!\w)', re.I)
ABBREVIATION = re.compile(r'(?:\b(?:mr|mrs|ms|dr|shri|smt|no|st|approx|rs|a\.m|p\.m|e\.g|i\.e)|\b[A-Z])\.$', re.I)


class IntakeError(ValueError):
    pass


def _docx(data):
    with ZipFile(io.BytesIO(data)) as archive:
        entry = archive.getinfo('word/document.xml')
        if entry.file_size > 5 * 1024 * 1024:
            raise IntakeError('The Word document contains too much XML.')
        xml = archive.read(entry)
    if b'\0' in xml or re.search(rb'<!\s*(DOCTYPE|ENTITY)', xml, re.I):
        raise IntakeError('Unsupported Word XML encoding or declarations.')
    body = ElementTree.fromstring(xml).find(f'{WORD}body')
    if body is None:
        raise IntakeError('The Word file has no document body.')
    if any(body.find(f'.//{WORD}{tag}') is not None for tag in ('ins', 'del', 'moveFrom', 'moveTo', 'altChunk', 'txbxContent')):
        raise IntakeError('Resolve tracked changes or move embedded text into the Word document body first.')
    paragraphs = []
    for paragraph in body.iter(f'{WORD}p'):
        parts = []
        for node in paragraph.iter():
            if node.tag == f'{WORD}t':
                parts.append(node.text or '')
            elif node.tag in (f'{WORD}br', f'{WORD}cr'):
                parts.append('\n')
            elif node.tag == f'{WORD}tab':
                parts.append('\t')
        paragraphs.append(''.join(parts))
    return '\n\n'.join(paragraphs)


def _legacy_doc(data):
    soffice = os.environ.get('SPANDAN_SOFFICE') or shutil.which('soffice')
    word = False
    if not soffice and os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r'Word.Application\CLSID'):
                word = True
        except FileNotFoundError:
            pass
        candidate = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'LibreOffice/program/soffice.com'
        if candidate.is_file():
            soffice = str(candidate)
    if not (word or soffice):
        raise IntakeError('Install Microsoft Word or LibreOffice to import .doc files, or save the file as .docx.')
    if not data.startswith(bytes.fromhex('D0CF11E0A1B11AE1')):
        raise IntakeError('The uploaded file is not a legacy Word document.')
    with tempfile.TemporaryDirectory(prefix='spandan-doc-', ignore_cleanup_errors=True) as temporary:
        folder = Path(temporary)
        source = folder / 'complaint.doc'
        output = folder / 'complaint.docx'
        source.write_bytes(data)
        if word:
            script = Path(__file__).with_name('legacy_word.ps1')
            command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                       '-File', str(script), str(source), str(output)]
        else:
            command = _libreoffice_command(soffice, source, folder)
        try:
            subprocess.run(command, capture_output=True, check=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            raise IntakeError('The local Word converter failed. Save this document as .docx and upload it again.') from error
        if not output.is_file() or output.stat().st_size > MAX_UPLOAD:
            raise IntakeError('The local converter did not produce a readable Word document.')
        return _docx(output.read_bytes())


def _libreoffice_command(soffice, source, folder):
    profile = folder / 'profile/user'
    profile.mkdir(parents=True)
    (profile / 'registrymodifications.xcu').write_text('''<?xml version="1.0"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting">
<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update">
<prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>
</oor:items>''', encoding='utf-8')
    return [soffice, f'-env:UserInstallation={profile.parent.as_uri()}', '--headless',
            '--convert-to', 'docx', '--outdir', str(folder), str(source)]


def extract(filename, data):
    """Return extracted text blocks and their source page, if available."""
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == '.pdf':
            if not data.startswith(b'%PDF-'):
                raise IntakeError('The uploaded file is not a PDF.')
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted or len(reader.pages) > 60:
                raise IntakeError('Upload an unlocked PDF with at most 60 pages.')
            blocks = []
            for index, page in enumerate(reader.pages, 1):
                contents = page.get_contents()
                if contents is not None and len(contents.get_data()) > 8 * 1024 * 1024:
                    raise IntakeError(f'PDF page {index} has an oversized content stream.')
                block = page.extract_text() or ''
                if not regex.search(r'\p{L}', block):
                    raise IntakeError(f'PDF page {index} has no extractable typed text. Use a text-based PDF or Word file.')
                blocks.append((index, block))
        elif suffix == '.docx':
            blocks = [(None, _docx(data))]
        elif suffix == '.doc':
            blocks = [(None, _legacy_doc(data))]
        elif suffix == '.txt':
            blocks = [(None, data.decode('utf-8-sig'))]
        else:
            raise IntakeError('Use a typed .pdf, .docx, .doc, or UTF-8 .txt file.')
    except IntakeError:
        raise
    except Exception as error:
        raise IntakeError('This document could not be read. Open it and save a fresh copy.') from error
    joined = '\n\n'.join(block for _, block in blocks)
    if len(joined) > MAX_TEXT:
        raise IntakeError(f'Documents can contain up to {MAX_TEXT:,} characters of extracted text.')
    if not regex.search(r'\p{L}', joined):
        raise IntakeError('The document does not contain readable typed text.')
    if regex.search(r'[\x00\ufffd\p{Co}]', joined):
        raise IntakeError('The document contains unreadable glyphs. Re-export it with an embedded Unicode text layer.')
    return blocks


def normalize(text):
    text = unicodedata.normalize('NFC', text)
    text = regex.sub(r'(?<!\p{Indic_Syllabic_Category=Virama})[\u200c\u200d](?!\p{Indic_Syllabic_Category=Virama})', '', text)
    return regex.sub(r'[\u200b\ufeff]', '', text)


def script_spans(text):
    spans = []
    for match in SCRIPT_PATTERN.finditer(text):
        label = match.lastgroup
        if spans and spans[-1]['script'] == label:
            spans[-1]['end'] = match.end()
        else:
            spans.append({'start': match.start(), 'end': match.end(), 'script': label})
    return spans


def segment(blocks):
    """Keep precise offsets into the extracted text, preserving original whitespace."""
    passages = []
    offset = 0
    for page, block in blocks:
        boundaries = []
        start = 0
        for match in re.finditer(r'[।॥!?]+[\u201d\u2019\"\']*|\.(?=\s|$)|\n\s*\n', block):
            end = match.end()
            if match.group() == '.' and ABBREVIATION.search(block[max(0, end - 12):end]):
                continue
            boundaries.append((start, end))
            start = end
        boundaries.append((start, len(block)))
        page_passages = []
        for start, end in boundaries:
            raw = block[start:end]
            start += len(raw) - len(raw.lstrip())
            end -= len(raw) - len(raw.rstrip())
            if end <= start:
                continue
            if page_passages and DEPENDENT.match(block[start:end]):
                page_passages[-1] = (page_passages[-1][0], end)
            else:
                page_passages.append((start, end))
        for start, end in page_passages:
            raw = block[start:end]
            passages.append({'id': f'H{len(passages) + 1:02}', 'page': page, 'start': offset + start,
                             'end': offset + end, 'source': raw, 'normalized': normalize(raw)})
        offset += len(block) + 2
    return passages
