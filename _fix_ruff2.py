# Fix remaining ruff E501 line-length and SIM222 issues in the TEST FILE only.
tpath = "tests/unit/test_phase7_availability.py"
with open(tpath, encoding="utf-8") as f:
    src = f.read()

# Fix E501: diminishing returns c1 line
old = '        c1 = ev.corroborate([_item(SourceReliability.UNVERIFIED, EvidenceType.INJURY, "doubtful", "a")])["confidence"]'
new = '        c1 = ev.corroborate([\n            _item(SourceReliability.UNVERIFIED, EvidenceType.INJURY, "doubtful", "a"),\n        ])["confidence"]'
assert old in src, "c1 marker not found"
src = src.replace(old, new)

# Fix E501: probabilities_in_bounds wrapper line
old = '        wrapper = AvailabilityAwareMinutesModel(base, _FakeAvailabilityProvider("questionable", 0.7))'
new = '        wrapper = AvailabilityAwareMinutesModel(\n            base, _FakeAvailabilityProvider("questionable", 0.7)\n        )'
assert old in src, "probabilities marker not found"
src = src.replace(old, new)

# Fix SIM222: or True
old = '        assert result.phase7_gw_average != result.baseline_gw_average or True'
new = '        assert result.phase7_gw_average != result.baseline_gw_average'
assert old in src, "sim222 marker not found"
src = src.replace(old, new)

with open(tpath, "w", encoding="utf-8") as f:
    f.write(src)
print("test file fixed")
