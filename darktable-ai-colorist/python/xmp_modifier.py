"""Darktable 5.6.2 XMP history and little-endian module parameters."""

import base64
import math
import struct
import xml.etree.ElementTree as ET
import zlib
from copy import deepcopy
from pathlib import Path

import exifread
import numpy as np

XMP = "adobe:ns:meta/"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
DT = "http://darktable.sf.net/"
for prefix, namespace in (("x", XMP), ("rdf", RDF), ("darktable", DT)):
    ET.register_namespace(prefix, namespace)


def rdf(name):
    return f"{{{RDF}}}{name}"


def dt(name):
    return f"{{{DT}}}{name}"


# Introspection versions and field order in src/iop at tag release-5.6.2.
EXPOSURE = struct.Struct("<iffffii")
TEMPERATURE = struct.Struct("<ffffi")
SIGMOID = struct.Struct("<ffffi8fi")
COLOR_BALANCE = struct.Struct("<32fi")
assert (EXPOSURE.size, TEMPERATURE.size, SIGMOID.size, COLOR_BALANCE.size) == (28, 20, 56, 132)

_grade = [0.0] * 32
_grade[12] = _grade[14] = 1.0
_grade[28] = _grade[30] = 0.1845
MODULES = {
    "exposure": (7, EXPOSURE, EXPOSURE.pack(0, 0, 0, 50, -4, 0, 1)),
    "temperature": (4, TEMPERATURE, TEMPERATURE.pack(1, 1, 1, math.inf, 2)),
    "sigmoid": (3, SIGMOID, SIGMOID.pack(1.5, 0, 100, 0.0152, 0, 100, *([0.0] * 7), 0)),
    "colorbalancergb": (5, COLOR_BALANCE, COLOR_BALANCE.pack(*_grade, 1)),
}

# Model units, C-struct offsets in 32-bit words, and scaling to model units.
FIELDS = {
    "exposure": ("exposure", 2, 1, -18, 18),
    "black_level": ("exposure", 1, 1, -1, 1),
    "temperature_k": ("temperature", None, 1, 1901, 25000),
    "tint": ("temperature", None, 1, 0.135, 2.326),
    "sigmoid_contrast": ("sigmoid", 0, 1, 0.1, 10),
    "sigmoid_skew": ("sigmoid", 1, 1, -1, 1),
    "global_vibrance": ("colorbalancergb", 29, 100, -100, 100),
    "shadows_luminance": ("colorbalancergb", 0, 100, -100, 100),
    "highlights_luminance": ("colorbalancergb", 6, 100, -100, 100),
}


def decode_blob(value: str) -> bytes:
    if value.startswith("gz"):
        # Darktable writes "gz", two decimal compression-factor digits, then zlib/Base64.
        return zlib.decompress(base64.b64decode(value[4:], validate=True))
    return bytes.fromhex(value)


def _property(node, name, default=None):
    key = dt(name)
    if key in node.attrib:
        return node.attrib[key]
    child = node.find(key)
    return child.text if child is not None else default


def _set(node, name, value):
    key = dt(name)
    child = node.find(key)
    if child is not None:
        child.text = str(value)
    else:
        node.set(key, str(value))


def _resource(item):
    description = item.find(rdf("Description"))
    return description if description is not None else item


def _blob(row, operation):
    latest, layout, default = MODULES[operation]
    if row is None:
        return default
    version = int(_property(row, "modversion"))
    value = decode_blob(_property(row, "params"))
    if version == latest and len(value) == layout.size:
        return value
    if operation == "exposure" and version == 6 and len(value) == 24:
        return value + struct.pack("<i", 0)
    if operation == "temperature" and version == 3 and len(value) == 16:
        return value[:12] + struct.pack("<fi", math.nan, -1)
    if operation == "sigmoid" and (version, len(value)) in {(1, 24), (2, 52)}:
        return value + default[len(value):]
    if operation == "colorbalancergb" and (version, len(value)) in {
        (1, 96), (2, 112), (3, 116), (4, 128)
    }:
        return value + default[len(value):128] + struct.pack("<i", 0)
    raise ValueError(
        f"{operation}: unsupported parameter version {version} ({len(value)} bytes). "
        "Open and resave this edit with Darktable 5.6.2."
    )


class History:
    def __init__(self, source: Path | None, is_raw: bool):
        if source is None:
            self.tree = ET.ElementTree(ET.Element(f"{{{XMP}}}xmpmeta"))
            rdf_root = ET.SubElement(self.tree.getroot(), rdf("RDF"))
            self.description = ET.SubElement(rdf_root, rdf("Description"), {rdf("about"): ""})
            _set(self.description, "xmp_version", 5)
            _set(self.description, "iop_order_version", 4 if is_raw else 5)
            _set(self.description, "auto_presets_applied", 0)
        else:
            self.tree = ET.parse(source)
            candidates = self.tree.getroot().iter(rdf("Description"))
            self.description = next((node for node in candidates if _property(node, "xmp_version")), None)
            if self.description is None or _property(self.description, "xmp_version") not in {"3", "4", "5"}:
                raise ValueError("Expected a Darktable XMP sidecar with structured history (version 3–5).")
        history = self.description.find(dt("history"))
        if history is None:
            history = ET.SubElement(self.description, dt("history"))
        self.sequence = history.find(rdf("Seq"))
        if self.sequence is None:
            self.sequence = ET.SubElement(history, rdf("Seq"))
        self.end = int(_property(self.description, "history_end", str(len(self.sequence))))
        self.active = {}
        for index, item in enumerate(self.sequence):
            row = _resource(item)
            if int(_property(row, "num", str(index))) < self.end:
                key = (_property(row, "operation"), int(_property(row, "multi_priority", "0")))
                self.active[key] = item

    def current(self, matrix):
        values = {}
        for name, (operation, offset, scale, _, _) in FIELDS.items():
            item = self.active.get((operation, 0))
            if offset is not None:
                row = _resource(item) if item is not None else None
                value = struct.unpack_from("<f", _blob(row, operation), offset * 4)[0]
                values[name] = round(value * scale, 5 if scale == 100 else 6)
        item = self.active.get(("temperature", 0))
        if item is not None and matrix is not None:
            gains = TEMPERATURE.unpack(_blob(_resource(item), "temperature"))[:len(matrix)]
            if all(math.isfinite(gain) and gain > 0 for gain in gains):
                kelvin, tint = _white_balance_settings(matrix, gains)
                values.update(temperature_k=kelvin, tint=tint)
        return values

    def enabled(self, operation):
        item = self.active.get((operation, 0))
        return item is not None and _property(_resource(item), "enabled") == "1"

    def grading_context(self):
        # These are decoded from the same history that produced the preview.
        controls = {}
        modules = []
        for (operation, instance), item in sorted(self.active.items()):
            row = _resource(item)
            enabled = _property(row, "enabled") == "1"
            modules.append({"operation": operation, "instance": instance, "enabled": enabled})
            if not enabled or instance != 0 or operation not in MODULES:
                continue
            _, layout, _ = MODULES[operation]
            values = layout.unpack(_blob(row, operation))
            if operation == "exposure":
                controls[operation] = {
                    "mode": {0: "manual", 1: "automatic"}.get(values[0], "unknown"),
                    "compensate_exposure_bias": bool(values[5]),
                    "compensate_highlight_preservation": bool(values[6]),
                }
            elif operation == "temperature":
                controls[operation] = {
                    "preset": {0: "as shot", 1: "spot", 2: "user defined", 3: "camera reference",
                               4: "as shot to reference"}.get(values[4], "other"),
                    "rgb_channel_gains": [round(value, 5) if math.isfinite(value) else None
                                          for value in values[:3]],
                }
            elif operation == "sigmoid":
                controls[operation] = {
                    "color_processing": {0: "per channel", 1: "RGB ratio"}.get(values[4], "unknown"),
                    "target_white_pct": round(values[2], 4), "target_black_pct": round(values[3], 4),
                    "hue_preservation_pct": round(values[5], 4),
                }
            elif operation == "colorbalancergb":
                controls[operation] = {
                    name: round(values[index] * 100, 4)
                    for name, index in (("midtones_luminance_pct", 3), ("global_luminance_pct", 9),
                                        ("global_chroma_pct", 17), ("global_saturation_pct", 19),
                                        ("contrast_pct", 31))
                }
        masks = self.description.find(f"{dt('masks_history')}/{rdf('Seq')}")
        return {
            "modules_recorded_in_xmp": modules,
            "recorded_settings": controls,
            "has_mask_records": masks is not None and any(
                int(_property(_resource(item), "mask_num", "-1")) < self.end for item in masks
            ),
        }

    def _discard_redo(self):
        masks = self.description.find(f"{dt('masks_history')}/{rdf('Seq')}")
        for sequence, number in ((self.sequence, "num"), (masks, "mask_num")):
            if sequence is not None:
                for item in list(sequence):
                    if int(_property(_resource(item), number, "-1")) >= self.end:
                        sequence.remove(item)

    def _append(self, operation, instance, item, blob=None):
        clone = deepcopy(item) if item is not None else ET.Element(rdf("li"))
        row = _resource(clone)
        if item is None:
            _set(row, "operation", operation)
            _set(row, "multi_priority", instance)
            _set(row, "multi_name", "")
            _set(row, "multi_name_hand_edited", 0)
        _set(row, "num", self.end)
        _set(row, "enabled", int(blob is not None))
        if blob is not None:
            _set(row, "modversion", MODULES[operation][0])
            _set(row, "params", blob.hex())
        self.sequence.append(clone)
        self.active[operation, instance] = clone
        self.end += 1

    def write(self, output: Path, changes: dict, matrix, current: dict):
        if changes:
            self._discard_redo()
        for operation, (_, layout, _) in MODULES.items():
            fields = {key: value for key, value in changes.items() if FIELDS[key][0] == operation}
            if not fields:
                continue
            item = self.active.get((operation, 0))
            source = _resource(item) if item is not None else None
            if operation == "temperature":
                kelvin = fields.get("temperature_k", current.get("temperature_k"))
                tint = fields.get("tint", current.get("tint"))
                if matrix is None or kelvin is None or tint is None:
                    raise ValueError("White balance needs a camera matrix and both Kelvin and tint.")
                blob = pack_temperature(kelvin, tint, matrix)
            else:
                values = list(layout.unpack(_blob(source, operation)))
                for key, value in fields.items():
                    _, offset, scale, _, _ = FIELDS[key]
                    values[offset] = value / scale
                if operation == "exposure" and "exposure" in fields:
                    values[0] = 0  # Manual exposure makes the requested EV effective.
                blob = layout.pack(*values)
            if operation == "sigmoid":
                for (other, instance), old in list(self.active.items()):
                    if other in {"filmicrgb", "basecurve"} and _property(_resource(old), "enabled") == "1":
                        self._append(other, instance, old)
            self._append(operation, 0, item, blob)
        if changes:
            _set(self.description, "history_end", self.end)
            for name in ("history_basic_hash", "history_auto_hash", "history_current_hash"):
                self.description.attrib.pop(dt(name), None)
                child = self.description.find(dt(name))
                if child is not None:
                    self.description.remove(child)
        self.tree.write(output, encoding="utf-8", xml_declaration=True)


def _illuminant_xyz(kelvin):
    if kelvin < 4000:
        # Planck's spectrum. Its common scale cancels during normalization.
        power = 1 / (_WAVELENGTHS**5 * np.expm1(0.014387769599838156 / (_WAVELENGTHS * kelvin)))
    else:
        if kelvin <= 7000:
            x = ((-4.6070e9 / kelvin + 2.9678e6) / kelvin + 99.11) / kelvin + 0.244063
        else:
            x = ((-2.0064e9 / kelvin + 1.9018e6) / kelvin + 247.48) / kelvin + 0.237040
        y = -3 * x * x + 2.87 * x - 0.275
        denominator = 0.0241 + 0.2562 * x - 0.7341 * y
        m1 = (-1.3515 - 1.7703 * x + 5.9114 * y) / denominator
        m2 = (0.0300 - 31.4424 * x + 30.0717 * y) / denominator
        power = _SPECTRA[:, 3:] @ (1.0, m1, m2)
    xyz = power @ _SPECTRA[:, :3]
    return xyz / xyz.max()


def pack_temperature(kelvin, tint, matrix):
    xyz = _illuminant_xyz(kelvin)
    xyz[1] /= tint
    response = matrix @ xyz
    if not np.all(np.isfinite(response) & (response > 0)):
        raise ValueError("This Kelvin/tint combination produces invalid camera white balance coefficients.")
    gains = response[1] / response
    return TEMPERATURE.pack(*gains[:3], gains[3] if len(gains) == 4 else math.inf, 2)


def _white_balance_settings(matrix, gains):
    xyz = np.linalg.pinv(matrix) @ (1 / np.asarray(gains))
    low, high = 1901.0, 25000.0
    if abs(xyz[0] * xyz[1]) < 1e-12:
        return 6500, 1.0
    while high - low > 1:
        temp = (low + high) / 2
        candidate = _illuminant_xyz(temp)
        if candidate[2] / candidate[0] > xyz[2] / xyz[0]:
            high = temp
        else:
            low = temp
    tint = (candidate[1] / candidate[0]) / (xyz[1] / xyz[0])
    return round((low + high) / 2), round(float(min(2.326, max(0.135, tint))), 3)


_NON_RAW = {".jpeg", ".jpg", ".jxl", ".png", ".tif", ".tiff", ".webp", ".avif", ".heif",
            ".heic", ".exr", ".bmp", ".ppm", ".pfm", ".gif", ".jp2"}
_SRGB = np.asarray(((3.2404542, -1.5371385, -0.4985314),
                    (-0.9692660, 1.8760108, 0.0415560),
                    (0.0556434, -0.2040259, 1.0572252)))


def _exif(path):
    with path.open("rb") as handle:
        return exifread.process_file(handle, details=False, extract_thumbnail=False)


def _dng_matrix(tags):
    # ExifRead reports unknown DNG tags by numeric code.
    def tag(code, size, default):
        value = tags.get(f"Image Tag 0x{code:04X}")
        return [float(item.decimal() if hasattr(item, "decimal") else item) for item in value.values] \
            if value and len(value.values) == size else default

    identity = np.eye(3, dtype=np.float32)
    illuminant_ids = (0xC65A, 0xC65B, 0xCD31)
    matrix_ids = (0xC621, 0xC622, 0xCD33)
    calibration_ids = (0xC623, 0xC624, 0xCD32)
    matrices = [tag(code, 9, None) for code in matrix_ids]
    illuminants = [int(tag(code, 1, [0])[0]) for code in illuminant_ids]
    illuminants = [21 if code >= len(_ILLUMINANTS) - 1 else max(0, code) for code in illuminants]
    selected, selected_temp, distance = -1, 0, 6500
    for index, code in enumerate(illuminants):
        temp, _ = _ILLUMINANTS[code]
        if temp > selected_temp and abs(temp - 6500) <= distance:
            selected, selected_temp, distance = index, temp, abs(temp - 6500)
    if selected < 0:
        selected = next((i for i, matrix in enumerate(matrices) if illuminants[i] == 0 and matrix), -1)
    if selected < 0:
        return None
    adaptation = _ILLUMINANTS[illuminants[selected] or 21][1]
    balance = np.diag(np.asarray(tag(0xC627, 3, [1, 1, 1]), dtype=np.float32))
    calibration = tag(calibration_ids[selected], 9, identity) if matrices[selected] else identity
    matrix = np.asarray(matrices[selected] or identity, dtype=np.float32).reshape(3, 3)
    return (np.asarray(adaptation, dtype=np.float32).reshape(3, 3) @
            ((balance @ np.asarray(calibration, dtype=np.float32).reshape(3, 3)) @ matrix)).astype(float)


def camera_profile(raw: Path, cameras: Path | None, is_raw: bool | None = None,
                   maker: str = "", model: str = ""):
    is_raw = raw.suffix.lower() not in _NON_RAW if is_raw is None else is_raw
    if not is_raw:
        return False, _SRGB
    tags = _exif(raw)
    if raw.suffix.lower() == ".dng" or "Image Tag 0xC612" in tags:
        matrix = _dng_matrix(tags)
        if matrix is not None:
            return True, matrix
    maker = (maker or str(tags.get("Image Make", ""))).strip().casefold()
    model = (model or str(tags.get("Image Model", ""))).strip().casefold()
    if cameras is None or not cameras.is_file():
        return True, None
    matches = []
    for camera in ET.parse(cameras).iter("Camera"):
        identity = camera.find("ID")
        make = {camera.get("make", "").casefold()}
        names = {camera.get("model", "").casefold()}
        names.update((alias.text or "").casefold() for alias in camera.findall("Aliases/Alias"))
        if identity is not None:
            make.add(identity.get("make", "").casefold())
            names.update((identity.get("model", "").casefold(), (identity.text or "").casefold()))
        rows = camera.findall("ColorMatrices/ColorMatrix/ColorMatrixRow")
        if maker and model and maker in make and model in names and len(rows) in {3, 4}:
            rows.sort(key=lambda row: int(row.get("plane")))
            matches.append(tuple(tuple(float(value) / 10000 for value in row.text.split()) for row in rows))
    if len(set(matches)) != 1:
        return True, None
    return True, np.asarray(matches[0], dtype=np.float32).astype(float)


# CIE 1931 2° observer and CIE daylight components at 5 nm, as used by Darktable.
# Source: src/external/cie_colorimetric_tables.c, release-5.6.2.
_SPECTRA = np.array([
    (0.001368, 0.000039, 0.006450, 63.40, 38.50, 3.00),
    (0.002236, 0.000064, 0.010550, 64.60, 36.75, 2.10),
    (0.004243, 0.000120, 0.020050, 65.80, 35.00, 1.20),
    (0.007650, 0.000217, 0.036210, 80.30, 39.20, 0.05),
    (0.014310, 0.000396, 0.067850, 94.80, 43.40, -1.10),
    (0.023190, 0.000640, 0.110200, 99.80, 44.85, -0.80),
    (0.043510, 0.001210, 0.207400, 104.80, 46.30, -0.50),
    (0.077630, 0.002180, 0.371300, 105.35, 45.10, -0.60),
    (0.134380, 0.004000, 0.645600, 105.90, 43.90, -0.70),
    (0.214770, 0.007300, 1.039050, 101.35, 40.50, -0.95),
    (0.283900, 0.011600, 1.385600, 96.80, 37.10, -1.20),
    (0.328500, 0.016840, 1.622960, 105.35, 36.90, -1.90),
    (0.348280, 0.023000, 1.747060, 113.90, 36.70, -2.60),
    (0.348060, 0.029800, 1.782600, 119.75, 36.30, -2.75),
    (0.336200, 0.038000, 1.772110, 125.60, 35.90, -2.90),
    (0.318700, 0.048000, 1.744100, 125.55, 34.25, -2.85),
    (0.290800, 0.060000, 1.669200, 125.50, 32.60, -2.80),
    (0.251100, 0.073900, 1.528100, 123.40, 30.25, -2.70),
    (0.195360, 0.090980, 1.287640, 121.30, 27.90, -2.60),
    (0.142100, 0.112600, 1.041900, 121.30, 26.10, -2.60),
    (0.095640, 0.139020, 0.812950, 121.30, 24.30, -2.60),
    (0.057950, 0.169300, 0.616200, 117.40, 22.20, -2.20),
    (0.032010, 0.208020, 0.465180, 113.50, 20.10, -1.80),
    (0.014700, 0.258600, 0.353300, 113.30, 18.15, -1.65),
    (0.004900, 0.323000, 0.272000, 113.10, 16.20, -1.50),
    (0.002400, 0.407300, 0.212300, 111.95, 14.70, -1.40),
    (0.009300, 0.503000, 0.158200, 110.80, 13.20, -1.30),
    (0.029100, 0.608200, 0.111700, 108.65, 10.90, -1.25),
    (0.063270, 0.710000, 0.078250, 106.50, 8.60, -1.20),
    (0.109600, 0.793200, 0.057250, 107.65, 7.35, -1.10),
    (0.165500, 0.862000, 0.042160, 108.80, 6.10, -1.00),
    (0.225750, 0.914850, 0.029840, 107.05, 5.15, -0.75),
    (0.290400, 0.954000, 0.020300, 105.30, 4.20, -0.50),
    (0.359700, 0.980300, 0.013400, 104.85, 3.05, -0.40),
    (0.433450, 0.994950, 0.008750, 104.40, 1.90, -0.30),
    (0.512050, 1.000000, 0.005750, 102.20, 0.95, -0.15),
    (0.594500, 0.995000, 0.003900, 100.00, 0.00, 0.00),
    (0.678400, 0.978600, 0.002750, 98.00, -0.80, 0.10),
    (0.762100, 0.952000, 0.002100, 96.00, -1.60, 0.20),
    (0.842500, 0.915400, 0.001800, 95.55, -2.55, 0.35),
    (0.916300, 0.870000, 0.001650, 95.10, -3.50, 0.50),
    (0.978600, 0.816300, 0.001400, 92.10, -3.50, 1.30),
    (1.026300, 0.757000, 0.001100, 89.10, -3.50, 2.10),
    (1.056700, 0.694900, 0.001000, 89.80, -4.65, 2.65),
    (1.062200, 0.631000, 0.000800, 90.50, -5.80, 3.20),
    (1.045600, 0.566800, 0.000600, 90.40, -6.50, 3.65),
    (1.002600, 0.503000, 0.000340, 90.30, -7.20, 4.10),
    (0.938400, 0.441200, 0.000240, 89.35, -7.90, 4.40),
    (0.854450, 0.381000, 0.000190, 88.40, -8.60, 4.70),
    (0.751400, 0.321000, 0.000100, 86.20, -9.05, 4.90),
    (0.642400, 0.265000, 0.000050, 84.00, -9.50, 5.10),
    (0.541900, 0.217000, 0.000030, 84.55, -10.20, 5.90),
    (0.447900, 0.175000, 0.000020, 85.10, -10.90, 6.70),
    (0.360800, 0.138200, 0.000010, 83.50, -10.80, 7.00),
    (0.283500, 0.107000, 0.000000, 81.90, -10.70, 7.30),
    (0.218700, 0.081600, 0.000000, 82.25, -11.35, 7.95),
    (0.164900, 0.061000, 0.000000, 82.60, -12.00, 8.60),
    (0.121200, 0.044580, 0.000000, 83.75, -13.00, 9.20),
    (0.087400, 0.032000, 0.000000, 84.90, -14.00, 9.80),
    (0.063600, 0.023200, 0.000000, 83.10, -13.80, 10.00),
    (0.046770, 0.017000, 0.000000, 81.30, -13.60, 10.20),
    (0.032900, 0.011920, 0.000000, 76.60, -12.80, 9.25),
    (0.022700, 0.008210, 0.000000, 71.90, -12.00, 8.30),
    (0.015840, 0.005723, 0.000000, 73.10, -12.65, 8.95),
    (0.011359, 0.004102, 0.000000, 74.30, -13.30, 9.60),
    (0.008111, 0.002929, 0.000000, 75.35, -13.10, 9.05),
    (0.005790, 0.002091, 0.000000, 76.40, -12.90, 8.50),
    (0.004109, 0.001484, 0.000000, 69.85, -11.75, 7.75),
    (0.002899, 0.001047, 0.000000, 63.30, -10.60, 7.00),
    (0.002049, 0.000740, 0.000000, 67.50, -11.10, 7.30),
    (0.001440, 0.000520, 0.000000, 71.70, -11.60, 7.60),
    (0.001000, 0.000361, 0.000000, 74.35, -11.90, 7.80),
    (0.000690, 0.000249, 0.000000, 77.00, -12.20, 8.00),
    (0.000476, 0.000172, 0.000000, 71.10, -11.20, 7.35),
    (0.000332, 0.000120, 0.000000, 65.20, -10.20, 6.70),
    (0.000235, 0.000085, 0.000000, 56.45, -9.00, 5.95),
    (0.000166, 0.000060, 0.000000, 47.70, -7.80, 5.20),
    (0.000117, 0.000042, 0.000000, 58.15, -9.50, 6.30),
    (0.000083, 0.000030, 0.000000, 68.60, -11.20, 7.40),
    (0.000059, 0.000021, 0.000000, 66.80, -10.80, 7.10),
    (0.000042, 0.000015, 0.000000, 65.00, -10.40, 6.80),
], dtype=float)
_WAVELENGTHS = np.arange(380, 781, 5) * 1e-9

# DNG illuminant temperatures and Bradford corrections from src/common/exif.cc.
_ILLUMINANTS = (
    (0, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),  # Unknown Illuminant
    (5500, (0.9726856, -0.0135482, 0.0361731, -0.0167463, 1.0049102, 0.0120598, 0.0070026, -0.0116372, 1.1869548)),  # Daylight
    (4230, (0.9212269, -0.0449128, 0.1211620, -0.0553723, 1.0277243, 0.0403563, 0.0235086, -0.0391019, 1.6390644)),  # Fluorescent
    (2850, (0.8446965, -0.1179225, 0.3948108, -0.1366303, 1.1041226, 0.1291718, 0.0798489, -0.1348999, 3.1924009)),  # Tungsten
    (5500, (0.9726856, -0.0135482, 0.0361731, -0.0167463, 1.0049102, 0.0120598, 0.0070026, -0.0116372, 1.1869548)),  # Flash
    (0, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),  # Unknown Illuminant5
    (0, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),  # Unknown Illuminant6
    (0, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),  # Unknown Illuminant7
    (0, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),  # Unknown Illuminant8
    (5500, (0.9726856, -0.0135482, 0.0361731, -0.0167463, 1.0049102, 0.0120598, 0.0070026, -0.0116372, 1.1869548)),  # FineWeather
    (6500, (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)),  # CloudyWeather
    (7500, (1.0206905, 0.0091588, -0.0228796, 0.0115005, 0.9984917, -0.0076762, -0.0043619, 0.0072053, 0.8853432)),  # Shade
    (6430, (1.0096114, 0.0061501, 0.0068113, 0.0102539, 0.9888663, 0.0015575, 0.0023119, -0.0044823, 1.0525915)),  # DaylightFluorescent
    (5000, (0.9554129, -0.0231280, 0.0637169, -0.0283629, 1.0099053, 0.0211824, 0.0124188, -0.0206922, 1.3330592)),  # DayWhiteFluorescent
    (4150, (0.9147843, -0.0492842, 0.1202810, -0.0622085, 1.034984, 0.0404480, 0.0228014, -0.0375807, 1.6259804)),  # CoolWhiteFluorescent
    (3450, (0.8805388, -0.0774890, 0.2293784, -0.0932136, 1.0589267, 0.0757827, 0.0453660, -0.0760107, 2.2417979)),  # WhiteFluorescent
    (2940, (0.8488316, -0.1107439, 0.3471428, -0.1310107, 1.0986874, 0.1141548, 0.0694025, -0.1167541, 2.9109462)),  # WarmWhiteFluorescent
    (2850, (0.8446965, -0.1179225, 0.3948108, -0.1366303, 1.1041226, 0.1291718, 0.0798489, -0.1348999, 3.1924009)),  # StandardLightA
    (4871, (0.9415037, -0.0321240, 0.0584672, -0.0428238, 1.0250998, 0.0203309, 0.0101511, -0.0161170, 1.2847354)),  # StandardLightB
    (6774, (0.9904476, -0.0071683, -0.0116156, -0.0123712, 1.0155950, -0.0029282, -0.0035635, 0.0067697, 0.9181569)),  # StandardLightC
    (5500, (0.9726856, -0.0135482, 0.0361731, -0.0167463, 1.0049102, 0.0120598, 0.0070026, -0.0116372, 1.1869548)),  # D55
    (6500, (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)),  # D65
    (7500, (1.0206905, 0.0091588, -0.0228796, 0.0115005, 0.9984917, -0.0076762, -0.0043619, 0.0072053, 0.8853432)),  # D75
    (5000, (0.9555766, -0.0230393, 0.0631636, -0.0282895, 1.0099416, 0.0210077, 0.0122982, -0.0204830, 1.3299098)),  # D50
    (3200, (0.8663030, -0.0913083, 0.2771784, -0.1090504, 1.0746895, 0.0913841, 0.0550856, -0.0924636, 2.5119387)),  # ISOStudioTungsten
    (6500, (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)),  # Other illuminant
)
