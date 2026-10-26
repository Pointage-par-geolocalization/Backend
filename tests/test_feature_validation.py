import io, zipfile
import pytest
from app.feature_api import _filetype, ATTACH_TYPES, MAX_ATTACHMENT

def office(ext, member):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        z.writestr('[Content_Types].xml','types')
        z.writestr(member,'content')
    return b.getvalue()

@pytest.mark.parametrize('ext,data',[
    ('.pdf',b'%PDF-1.7 body'),('.jpg',b'\xff\xd8\xffabc'),('.jpeg',b'\xff\xd8\xffabc'),('.png',b'\x89PNG\r\n\x1a\nrest'),
])
def test_attachment_signature_allowlist(ext,data):
    assert ext in ATTACH_TYPES and _filetype(ext,data)

def test_office_archive_types_are_verified():
    assert _filetype('.xlsx',office('.xlsx','xl/workbook.xml'))
    assert _filetype('.docx',office('.docx','word/document.xml'))
    assert not _filetype('.xlsx',office('.xlsx','word/document.xml'))
    assert not _filetype('.docx',b'not a zip')

def test_attachment_limit_is_exactly_ten_mib():
    assert MAX_ATTACHMENT == 10 * 1024 * 1024
