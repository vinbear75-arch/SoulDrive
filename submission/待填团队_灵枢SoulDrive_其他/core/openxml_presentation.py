from __future__ import annotations

import html
import time
import zipfile
from pathlib import Path
from typing import Any


EMU_PER_POINT = 12_700
SLIDE_WIDTH = 960 * EMU_PER_POINT
SLIDE_HEIGHT = 540 * EMU_PER_POINT

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def render_presentation(plan: dict[str, Any], output_path: Path) -> dict[str, Any]:
    """Write a self-contained, editable PPTX without an Office installation."""
    slides = list(plan.get("slides") or [])
    if not slides:
        raise ValueError("presentation plan contains no slides")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            _write_package_files(archive, plan, slides)
        temporary_path.replace(output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return {
        "renderer": "SoulDrive OpenXML",
        "output_path": str(output_path),
        "slide_count": len(slides),
    }


def _write_package_files(
    archive: zipfile.ZipFile,
    plan: dict[str, Any],
    slides: list[dict[str, Any]],
) -> None:
    archive.writestr("[Content_Types].xml", _content_types(len(slides)))
    archive.writestr("_rels/.rels", ROOT_RELS)
    archive.writestr("docProps/core.xml", _core_properties(str(plan.get("title") or "SoulDrive Presentation")))
    archive.writestr("docProps/app.xml", _app_properties(len(slides)))
    archive.writestr("ppt/presentation.xml", _presentation(len(slides)))
    archive.writestr("ppt/_rels/presentation.xml.rels", _presentation_relationships(len(slides)))
    archive.writestr("ppt/slideMasters/slideMaster1.xml", SLIDE_MASTER)
    archive.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", SLIDE_MASTER_RELS)
    archive.writestr("ppt/slideLayouts/slideLayout1.xml", SLIDE_LAYOUT)
    archive.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", SLIDE_LAYOUT_RELS)
    archive.writestr("ppt/theme/theme1.xml", THEME)
    archive.writestr("ppt/presProps.xml", PRES_PROPS)
    archive.writestr("ppt/viewProps.xml", VIEW_PROPS)
    archive.writestr("ppt/tableStyles.xml", TABLE_STYLES)
    for index, slide in enumerate(slides, start=1):
        archive.writestr(f"ppt/slides/slide{index}.xml", _slide_xml(slide, plan, index))
        archive.writestr(f"ppt/slides/_rels/slide{index}.xml.rels", SLIDE_RELS)


def _slide_xml(slide: dict[str, Any], plan: dict[str, Any], slide_number: int) -> str:
    kind = str(slide.get("kind") or "content")
    shapes: list[str] = []
    shape_id = 2
    style = _style(plan.get("style"))
    font_family = style["font_family"]

    def rectangle(x: float, y: float, width: float, height: float, fill: str) -> None:
        nonlocal shape_id
        shapes.append(_rectangle(shape_id, x, y, width, height, fill))
        shape_id += 1

    def textbox(
        text: str,
        x: float,
        y: float,
        width: float,
        height: float,
        size: float,
        color: str,
        *,
        bold: bool = False,
        align: str = "l",
        bullets: bool = False,
    ) -> None:
        nonlocal shape_id
        shapes.append(
            _textbox(
                shape_id,
                text,
                x,
                y,
                width,
                height,
                size,
                color,
                bold=bold,
                align=align,
                bullets=bullets,
                font_family=font_family,
            )
        )
        shape_id += 1

    if kind == "cover":
        if style["cover_variant"] == "editorial_light":
            rectangle(0, 0, 960, 540, style["background"])
            rectangle(0, 0, 300, 540, style["primary"])
            rectangle(0, 0, 300, 18, style["accent"])
            textbox("SoulDrive", 58, 76, 180, 30, 15, "FFFFFF", bold=True)
            textbox(str(slide.get("eyebrow") or "专题汇报"), 58, 370, 190, 30, 15, style["accent"], bold=True)
            textbox(str(plan.get("generated_at_label") or ""), 58, 420, 190, 24, 10, "FFFFFF")
            rectangle(368, 94, 20, 20, style["accent"])
            textbox(str(slide.get("title") or "专题汇报"), 368, 142, 520, 150, 34, style["primary"], bold=True)
            textbox(str(slide.get("subtitle") or ""), 368, 326, 500, 78, 17, style["text"])
        else:
            rectangle(0, 0, 960, 540, style["primary"])
            rectangle(58, 70, 7, 330, style["accent"])
            textbox(str(slide.get("eyebrow") or "专题汇报"), 92, 78, 760, 30, 15, style["accent"], bold=True)
            textbox(str(slide.get("title") or "专题汇报"), 92, 132, 760, 155, 34, "FFFFFF", bold=True)
            textbox(str(slide.get("subtitle") or ""), 92, 314, 690, 80, 17, "FFFFFF")
            textbox(str(plan.get("generated_at_label") or ""), 92, 456, 420, 24, 10, style["accent"])
            textbox("SoulDrive", 758, 456, 110, 24, 11, "FFFFFF", bold=True, align="r")
    else:
        rectangle(0, 0, 960, 540, style["background"])
        rectangle(0, 0, 960, 12, style["primary"])
        if style["content_variant"] == "split_cards":
            rectangle(56, 38, 16, 16, style["accent"])
            textbox(str(slide.get("title") or "未命名"), 88, 30, 790, 54, 25, style["primary"], bold=True)
            if kind == "sources":
                rectangle(56, 126, 844, 300, style["surface"])
                textbox("\n".join(str(item) for item in slide.get("sources") or []), 82, 158, 790, 238, 16, style["text"], bullets=True)
            else:
                rectangle(56, 126, 520, 300, style["surface"])
                textbox("\n".join(str(item) for item in slide.get("points") or []), 82, 158, 468, 236, 17, style["text"], bullets=True)
                rectangle(610, 126, 290, 300, style["primary"])
                textbox("证据来源", 640, 158, 220, 28, 13, style["accent"], bold=True)
                textbox("\n".join(str(item) for item in slide.get("sources") or []), 640, 204, 220, 176, 11, "FFFFFF", bullets=True)
        else:
            textbox(str(slide.get("title") or "未命名"), 56, 38, 790, 54, 25, style["primary"], bold=True)
            rectangle(56, 98, 74, 4, style["accent"])
            if kind == "sources":
                textbox("\n".join(str(item) for item in slide.get("sources") or []), 58, 130, 830, 310, 16, style["text"], bullets=True)
            else:
                textbox("\n".join(str(item) for item in slide.get("points") or []), 58, 126, 620, 330, 17, style["text"], bullets=True)
                rectangle(718, 126, 182, 292, style["surface"])
                rectangle(718, 126, 182, 7, style["accent"])
                textbox("证据来源", 738, 151, 142, 26, 13, style["primary"], bold=True)
                textbox("\n".join(str(item) for item in slide.get("sources") or []), 738, 190, 142, 190, 11, "606965", bullets=True)
        rectangle(56, 482, 844, 1, "D7D5CC")
        textbox(str(slide.get("footer") or "SoulDrive 本地证据链"), 56, 492, 700, 22, 9, "606965")
        textbox(str(slide_number), 854, 492, 46, 22, 9, "606965", bold=True, align="r")

    return XML_DECL + (
        f'<p:sld xmlns:a="{A_NS}" xmlns:r="{R_NS}" xmlns:p="{P_NS}">'
        "<p:cSld><p:spTree>"
        "<p:nvGrpSpPr><p:cNvPr id=\"1\" name=\"\"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>"
        "<p:grpSpPr><a:xfrm><a:off x=\"0\" y=\"0\"/><a:ext cx=\"0\" cy=\"0\"/>"
        "<a:chOff x=\"0\" y=\"0\"/><a:chExt cx=\"0\" cy=\"0\"/></a:xfrm></p:grpSpPr>"
        + "".join(shapes)
        + "</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
    )


def _style(value: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "primary": "173F3B",
        "accent": "DAAB4A",
        "background": "F8F7F2",
        "text": "1C2624",
        "surface": "FFFFFF",
        "cover_variant": "dark",
        "content_variant": "evidence_split",
        "font_family": "Microsoft YaHei",
    }
    if not isinstance(value, dict):
        return defaults
    for key in ("primary", "accent", "background", "text", "surface"):
        color = str(value.get(key) or "").strip().lstrip("#").upper()
        if len(color) == 6 and all(char in "0123456789ABCDEF" for char in color):
            defaults[key] = color
    if str(value.get("cover_variant") or "").strip() in {"dark", "editorial_light"}:
        defaults["cover_variant"] = str(value["cover_variant"]).strip()
    if str(value.get("content_variant") or "").strip() in {"evidence_split", "split_cards"}:
        defaults["content_variant"] = str(value["content_variant"]).strip()
    font_family = str(value.get("font_family") or "").strip()
    if font_family and all(char.isalnum() or char in " ._-" for char in font_family) and len(font_family) <= 40:
        defaults["font_family"] = font_family
    return defaults


def _rectangle(shape_id: int, x: float, y: float, width: float, height: float, fill: str) -> str:
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="Rectangle {shape_id}"/>'
        "<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>"
        f"{_transform(x, y, width, height)}<a:prstGeom prst=\"rect\"><a:avLst/></a:prstGeom>"
        f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill><a:ln><a:noFill/></a:ln>'
        "</p:spPr></p:sp>"
    )


def _textbox(
    shape_id: int,
    text: str,
    x: float,
    y: float,
    width: float,
    height: float,
    size: float,
    color: str,
    *,
    bold: bool,
    align: str,
    bullets: bool,
    font_family: str,
) -> str:
    paragraphs = []
    lines = text.splitlines() or [""]
    for line in lines:
        bullet = '<a:buChar char="&#x2022;"/>' if bullets and line.strip() else "<a:buNone/>"
        margin = ' marL="342900" indent="-285750"' if bullets else ""
        run_properties = (
            f'<a:rPr lang="zh-CN" sz="{int(size * 100)}" b="{1 if bold else 0}" dirty="0">'
            f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
            f'<a:latin typeface="{font_family}"/><a:ea typeface="{font_family}"/>'
            "</a:rPr>"
        )
        paragraphs.append(
            f'<a:p><a:pPr algn="{align}"{margin}>{bullet}</a:pPr><a:r>{run_properties}'
            f"<a:t>{_escape(line)}</a:t></a:r>"
            f'<a:endParaRPr lang="zh-CN" sz="{int(size * 100)}"/></a:p>'
        )
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="TextBox {shape_id}"/>'
        '<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr>'
        f"{_transform(x, y, width, height)}<a:prstGeom prst=\"rect\"><a:avLst/></a:prstGeom>"
        "<a:noFill/><a:ln><a:noFill/></a:ln></p:spPr><p:txBody>"
        '<a:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0" anchor="t"/>'
        "<a:lstStyle/>" + "".join(paragraphs) + "</p:txBody></p:sp>"
    )


def _transform(x: float, y: float, width: float, height: float) -> str:
    return (
        "<a:xfrm>"
        f'<a:off x="{int(x * EMU_PER_POINT)}" y="{int(y * EMU_PER_POINT)}"/>'
        f'<a:ext cx="{int(width * EMU_PER_POINT)}" cy="{int(height * EMU_PER_POINT)}"/>'
        "</a:xfrm>"
    )


def _escape(value: str) -> str:
    return html.escape(str(value), quote=False)


def _content_types(slide_count: int) -> str:
    slide_overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for index in range(1, slide_count + 1)
    )
    return XML_DECL + (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        '<Override PartName="/ppt/presProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presProps+xml"/>'
        '<Override PartName="/ppt/viewProps.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.viewProps+xml"/>'
        '<Override PartName="/ppt/tableStyles.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.tableStyles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        f"{slide_overrides}</Types>"
    )


def _presentation(slide_count: int) -> str:
    slide_ids = "".join(
        f'<p:sldId id="{255 + index}" r:id="rId{index + 4}"/>' for index in range(1, slide_count + 1)
    )
    return XML_DECL + (
        f'<p:presentation xmlns:a="{A_NS}" xmlns:r="{R_NS}" xmlns:p="{P_NS}">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f"<p:sldIdLst>{slide_ids}</p:sldIdLst>"
        f'<p:sldSz cx="{SLIDE_WIDTH}" cy="{SLIDE_HEIGHT}" type="screen16x9"/>'
        '<p:notesSz cx="6858000" cy="9144000"/><p:defaultTextStyle/></p:presentation>'
    )


def _presentation_relationships(slide_count: int) -> str:
    slides = "".join(
        f'<Relationship Id="rId{index + 4}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{index}.xml"/>'
        for index in range(1, slide_count + 1)
    )
    return XML_DECL + (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/presProps" Target="presProps.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/viewProps" Target="viewProps.xml"/>'
        '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/tableStyles" Target="tableStyles.xml"/>'
        f"{slides}</Relationships>"
    )


def _core_properties(title: str) -> str:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return XML_DECL + (
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{_escape(title)}</dc:title><dc:creator>SoulDrive</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified></cp:coreProperties>'
    )


def _app_properties(slide_count: int) -> str:
    return XML_DECL + (
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        f"<Application>SoulDrive</Application><PresentationFormat>Widescreen</PresentationFormat><Slides>{slide_count}</Slides>"
        "<Notes>0</Notes><HiddenSlides>0</HiddenSlides><ScaleCrop>false</ScaleCrop>"
        "<Company></Company><LinksUpToDate>false</LinksUpToDate><SharedDoc>false</SharedDoc>"
        "<HyperlinksChanged>false</HyperlinksChanged><AppVersion>1.0</AppVersion></Properties>"
    )


XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
ROOT_RELS = XML_DECL + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>'
SLIDE_RELS = XML_DECL + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>'
SLIDE_LAYOUT_RELS = XML_DECL + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>'
SLIDE_MASTER_RELS = XML_DECL + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/></Relationships>'

GROUP_TREE = '<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree>'
SLIDE_LAYOUT = XML_DECL + f'<p:sldLayout xmlns:a="{A_NS}" xmlns:r="{R_NS}" xmlns:p="{P_NS}" type="blank" preserve="1"><p:cSld name="Blank">{GROUP_TREE}</p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>'
SLIDE_MASTER = XML_DECL + f'<p:sldMaster xmlns:a="{A_NS}" xmlns:r="{R_NS}" xmlns:p="{P_NS}"><p:cSld name="SoulDrive Master">{GROUP_TREE}</p:cSld><p:clrMap accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" bg1="lt1" bg2="lt2" folHlink="folHlink" hlink="hlink" tx1="dk1" tx2="dk2"/><p:sldLayoutIdLst><p:sldLayoutId id="1" r:id="rId1"/></p:sldLayoutIdLst><p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles></p:sldMaster>'
THEME = XML_DECL + f'<a:theme xmlns:a="{A_NS}" name="SoulDrive"><a:themeElements><a:clrScheme name="SoulDrive"><a:dk1><a:srgbClr val="1C2624"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="173F3B"/></a:dk2><a:lt2><a:srgbClr val="F8F7F2"/></a:lt2><a:accent1><a:srgbClr val="173F3B"/></a:accent1><a:accent2><a:srgbClr val="DAAB4A"/></a:accent2><a:accent3><a:srgbClr val="54756E"/></a:accent3><a:accent4><a:srgbClr val="B45F4D"/></a:accent4><a:accent5><a:srgbClr val="466A8A"/></a:accent5><a:accent6><a:srgbClr val="766A8A"/></a:accent6><a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme><a:fontScheme name="SoulDrive"><a:majorFont><a:latin typeface="Microsoft YaHei"/><a:ea typeface="Microsoft YaHei"/><a:cs typeface="Arial"/></a:majorFont><a:minorFont><a:latin typeface="Microsoft YaHei"/><a:ea typeface="Microsoft YaHei"/><a:cs typeface="Arial"/></a:minorFont></a:fontScheme><a:fmtScheme name="SoulDrive"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="accent1"/></a:solidFill><a:solidFill><a:schemeClr val="accent2"/></a:solidFill></a:fillStyleLst><a:lnStyleLst><a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="12700"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="lt1"/></a:solidFill><a:solidFill><a:schemeClr val="lt2"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>'
PRES_PROPS = XML_DECL + f'<p:presentationPr xmlns:a="{A_NS}" xmlns:r="{R_NS}" xmlns:p="{P_NS}"/>'
VIEW_PROPS = XML_DECL + f'<p:viewPr xmlns:a="{A_NS}" xmlns:r="{R_NS}" xmlns:p="{P_NS}" lastView="sldView"><p:normalViewPr><p:restoredLeft sz="15620"/><p:restoredTop sz="94660"/></p:normalViewPr><p:slideViewPr><p:cSldViewPr snapToGrid="0" snapToObjects="1"><p:cViewPr varScale="1"><p:scale><a:sx n="1" d="1"/><a:sy n="1" d="1"/></p:scale><p:origin x="0" y="0"/></p:cViewPr></p:cSldViewPr></p:slideViewPr><p:notesTextViewPr><p:cViewPr><p:scale><a:sx n="1" d="1"/><a:sy n="1" d="1"/></p:scale><p:origin x="0" y="0"/></p:cViewPr></p:notesTextViewPr></p:viewPr>'
TABLE_STYLES = XML_DECL + f'<a:tblStyleLst xmlns:a="{A_NS}" def="{{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}}"/>'
