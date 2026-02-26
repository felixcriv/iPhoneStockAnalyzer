"""Validate data/iphone_suppliers.json against the expected schema.

Exits non-zero and prints all errors found so the CI step is informative.
"""

import json
import sys
from pathlib import Path

VALID_CATEGORIES = {
    "Semiconductor", "Display", "Optics", "Power",
    "Storage", "RF", "Sensor", "Mechanical", "Audio", "Assembly", "Connector",
}
VALID_CONFIDENCE = {"confirmed", "strong_indication", "inferred"}
REQUIRED_METADATA = {"schema_version", "data_version", "last_updated", "iphone_models", "description", "sources"}
REQUIRED_SUPPLIER = {"company_name", "short_name", "supplier_id", "country", "publicly_traded", "role", "what_they_supply", "relationship_confidence"}
REQUIRED_COMPONENT = {"component_id", "component_name", "category", "description", "direct_suppliers", "indirect_suppliers"}


def err(errors: list[str], msg: str) -> None:
    errors.append(msg)


def validate_supplier(s: dict, path: str, errors: list[str], indirect: bool = False) -> None:
    missing = REQUIRED_SUPPLIER - s.keys()
    if missing:
        err(errors, f"{path}: missing fields {sorted(missing)}")
        return

    if not isinstance(s["company_name"], str) or not s["company_name"]:
        err(errors, f"{path}.company_name: must be a non-empty string")
    if not isinstance(s["publicly_traded"], bool):
        err(errors, f"{path}.publicly_traded: must be a boolean")
    if s["relationship_confidence"] not in VALID_CONFIDENCE:
        err(errors, f"{path}.relationship_confidence: got {s['relationship_confidence']!r}, expected one of {sorted(VALID_CONFIDENCE)}")
    if indirect:
        if "supplies_to" not in s:
            err(errors, f"{path}: indirect supplier missing 'supplies_to'")
        elif not isinstance(s["supplies_to"], list) or not s["supplies_to"]:
            err(errors, f"{path}.supplies_to: must be a non-empty list")


def validate(data: dict) -> list[str]:
    errors: list[str] = []

    # metadata
    if "metadata" not in data:
        err(errors, "root: missing 'metadata'")
    else:
        missing = REQUIRED_METADATA - data["metadata"].keys()
        if missing:
            err(errors, f"metadata: missing fields {sorted(missing)}")

    # components
    if "components" not in data:
        err(errors, "root: missing 'components'")
        return errors

    if not isinstance(data["components"], list) or len(data["components"]) == 0:
        err(errors, "components: must be a non-empty array")
        return errors

    seen_ids: set[str] = set()
    for i, comp in enumerate(data["components"]):
        p = f"components[{i}]"
        missing = REQUIRED_COMPONENT - comp.keys()
        if missing:
            err(errors, f"{p}: missing fields {sorted(missing)}")
            continue

        cid = comp.get("component_id", "")
        if cid in seen_ids:
            err(errors, f"{p}: duplicate component_id {cid!r}")
        seen_ids.add(cid)

        if comp["category"] not in VALID_CATEGORIES:
            err(errors, f"{p}.category: got {comp['category']!r}, expected one of {sorted(VALID_CATEGORIES)}")

        if not isinstance(comp["direct_suppliers"], list) or len(comp["direct_suppliers"]) == 0:
            err(errors, f"{p}.direct_suppliers: must be a non-empty array")
        else:
            for j, s in enumerate(comp["direct_suppliers"]):
                validate_supplier(s, f"{p}.direct_suppliers[{j}]", errors, indirect=False)

        if not isinstance(comp["indirect_suppliers"], list) or len(comp["indirect_suppliers"]) == 0:
            err(errors, f"{p}.indirect_suppliers: must be a non-empty array")
        else:
            direct_ids = {s.get("supplier_id") for s in comp.get("direct_suppliers", [])}
            for j, s in enumerate(comp["indirect_suppliers"]):
                validate_supplier(s, f"{p}.indirect_suppliers[{j}]", errors, indirect=True)
                for ref in s.get("supplies_to", []):
                    if ref not in direct_ids:
                        err(errors, f"{p}.indirect_suppliers[{j}].supplies_to: {ref!r} is not a known direct supplier_id")

    return errors


def main() -> int:
    path = Path(__file__).parent.parent / "data" / "iphone_suppliers.json"
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"INVALID JSON: {exc}")
        return 1

    errors = validate(data)
    if errors:
        print(f"Found {len(errors)} validation error(s):\n")
        for e in errors:
            print(f"  ✗ {e}")
        return 1

    comp_count = len(data.get("components", []))
    print(f"✓ {path.name} is valid ({comp_count} components)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
