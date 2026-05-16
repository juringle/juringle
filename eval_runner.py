import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
EVAL_CASES_PATH = ROOT / "eval_cases.json"
STOCK_DB_PATH = ROOT / "stock_db.json"
STOCK_CANDIDATE_DB_PATH = ROOT / "stock_candidate_db.json"
ANALYZE_PATH = "/analyze"
ANALYZE_URL_FIELD = "url"
ANALYZE_CONTENT_TYPE = "application/json"


def load_json(path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def stock_name(item):
    return item.get("name", "") if isinstance(item, dict) else str(item)


def build_name_maps(stock_db, candidate_db):
    stock_name_to_ticker = {}
    candidate_name_to_sector = {}
    candidate_name_to_ticker = {}

    for ticker, item in stock_db.items():
        name = stock_name(item)
        if name:
            stock_name_to_ticker[name] = ticker

    for ticker, item in candidate_db.items():
        name = item.get("name", "")
        if name:
            candidate_name_to_sector[name] = item.get("sector", "")
            candidate_name_to_ticker[name] = ticker

    return {
        "stock_name_to_ticker": stock_name_to_ticker,
        "candidate_name_to_sector": candidate_name_to_sector,
        "candidate_name_to_ticker": candidate_name_to_ticker,
    }


def validate_cases(cases, maps):
    required_fields = [
        "id",
        "title",
        "url",
        "article_type",
        "expected_sectors",
        "expected_subthemes",
        "preferred_candidates",
        "acceptable_candidates",
        "must_exclude_candidates",
        "direct_listed_entities",
        "mentioned_entities",
        "notes",
    ]
    forbidden_fields = ["must_include_candidates", "direct_entities"]
    stock_names = set(maps["stock_name_to_ticker"])
    candidate_names = set(maps["candidate_name_to_sector"])

    ids = [case.get("id") for case in cases]
    duplicate_ids = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    missing_fields = []
    forbidden_present = []
    stock_missing = []
    candidate_missing = []

    for case in cases:
        case_id = case.get("id", "<NO_ID>")
        missing = [field for field in required_fields if field not in case]
        if missing:
            missing_fields.append({"id": case_id, "fields": missing})

        forbidden = [field for field in forbidden_fields if field in case]
        if forbidden:
            forbidden_present.append({"id": case_id, "fields": forbidden})

        for field in [
            "preferred_candidates",
            "acceptable_candidates",
            "must_exclude_candidates",
            "direct_listed_entities",
        ]:
            for name in case.get(field, []):
                if name not in stock_names:
                    stock_missing.append({"id": case_id, "field": field, "name": name})

        for field in ["preferred_candidates", "acceptable_candidates"]:
            for name in case.get(field, []):
                if name not in candidate_names:
                    candidate_missing.append({"id": case_id, "field": field, "name": name})

    return {
        "case_count": len(cases),
        "duplicate_ids": duplicate_ids,
        "missing_fields": missing_fields,
        "forbidden_fields": forbidden_present,
        "stock_missing": stock_missing,
        "candidate_missing": candidate_missing,
        "ok": not (
            duplicate_ids
            or missing_fields
            or forbidden_present
            or stock_missing
            or candidate_missing
        ),
    }


def select_cases(cases, case_id=None, limit=None):
    selected = cases
    if case_id:
        selected = [case for case in selected if case.get("id") == case_id]
    if limit is not None:
        selected = selected[:limit]
    return selected


def run_analyze(case, index):
    import app as juringle_app

    client = juringle_app.app.test_client()
    response = client.post(
        ANALYZE_PATH,
        data=json.dumps({ANALYZE_URL_FIELD: case["url"]}, ensure_ascii=False),
        content_type=ANALYZE_CONTENT_TYPE,
        headers={"Accept": "application/json"},
        environ_base={"REMOTE_ADDR": f"10.77.0.{index + 1}"},
    )
    payload = response.get_json(silent=True) or {}
    if "error" in payload:
        return response.status_code, None, payload["error"]

    result_text = payload.get("result", "")
    try:
        result = json.loads(result_text)
    except json.JSONDecodeError as exc:
        return response.status_code, None, f"result JSON parse error: {exc}"
    return response.status_code, result, None


def candidate_names_from_good(result):
    if not result:
        return []
    return [
        stock.get("name", "")
        for stock in result.get("good", [])
        if stock.get("name")
    ]


def classify_noise(metrics):
    if metrics["error"] or not metrics["good"]:
        return "응답 실패"
    if metrics["must_exclude_violations"]:
        return "must_exclude 위반"
    if metrics["direct_entity_violations"]:
        return "direct entity 추천"
    if not metrics["expected_sector_hit"]:
        return "expected sector miss"

    expected_sectors = set(metrics["expected_sectors"])
    unmatched_outside_expected = [
        name
        for name in metrics["unmatched_good_candidates"]
        if metrics["good_sector_map"].get(name, "") not in expected_sectors
    ]
    if metrics["expected_sector_precision"] < 0.5 or len(unmatched_outside_expected) >= 2:
        return "섹터 오탐"
    if metrics["good_precision_estimate"] < 0.5:
        return "preferred/acceptable 후보 부족"
    if (
        metrics["good_precision_estimate"] >= 0.67
        and not metrics["must_exclude_violations"]
        and not metrics["direct_entity_violations"]
        and metrics["expected_sector_precision"] >= 0.67
    ):
        return "좋은 결과"
    return "부분 성공"


def evaluate_case(case, result, status_code, error, maps):
    good_names = candidate_names_from_good(result)
    preferred = set(case.get("preferred_candidates", []))
    acceptable = set(case.get("acceptable_candidates", []))
    preferred_or_acceptable = preferred | acceptable
    must_exclude = set(case.get("must_exclude_candidates", []))
    direct_entities = set(case.get("direct_listed_entities", []))
    expected_sectors = set(case.get("expected_sectors", []))
    name_to_sector = maps["candidate_name_to_sector"]

    preferred_hits = [name for name in good_names if name in preferred]
    acceptable_hits = [name for name in good_names if name in preferred_or_acceptable]
    must_exclude_violations = [name for name in good_names if name in must_exclude]
    direct_entity_violations = [name for name in good_names if name in direct_entities]
    good_sectors = [
        name_to_sector[name]
        for name in good_names
        if name in name_to_sector and name_to_sector[name]
    ]
    expected_sector_hit = bool(expected_sectors.intersection(good_sectors))
    good_sector_map = {
        name: name_to_sector.get(name, "")
        for name in good_names
    }
    unmatched_good_candidates = [
        name
        for name in good_names
        if name not in preferred_or_acceptable
        and name not in must_exclude
        and name not in direct_entities
    ]
    good_precision_estimate = (
        len(acceptable_hits) / len(good_names)
        if good_names else 0.0
    )
    expected_sector_precision = (
        sum(1 for name in good_names if good_sector_map.get(name, "") in expected_sectors)
        / len(good_names)
        if good_names else 0.0
    )

    metrics = {
        "id": case["id"],
        "title": case["title"],
        "status_code": status_code,
        "error": error,
        "good": good_names,
        "good_sectors": good_sectors,
        "good_sector_map": good_sector_map,
        "preferred_hits": preferred_hits,
        "acceptable_hits": acceptable_hits,
        "unmatched_good_candidates": unmatched_good_candidates,
        "must_exclude_violations": must_exclude_violations,
        "direct_entity_violations": direct_entity_violations,
        "expected_sectors": sorted(expected_sectors),
        "expected_sector_hit": expected_sector_hit,
        "expected_sector_precision": round(expected_sector_precision, 3),
        "good_precision_estimate": round(good_precision_estimate, 3),
    }
    metrics["noise_type"] = classify_noise(metrics)
    return metrics


def print_dry_run_summary(cases, validation):
    print("DRY RUN")
    print(f"- cases: {validation['case_count']}")
    print(f"- duplicate ids: {validation['duplicate_ids'] or 'none'}")
    print(f"- missing fields: {validation['missing_fields'] or 'none'}")
    print(f"- forbidden fields: {validation['forbidden_fields'] or 'none'}")
    print(f"- stock_db missing names: {validation['stock_missing'] or 'none'}")
    print(f"- candidate_db missing preferred/acceptable: {validation['candidate_missing'] or 'none'}")
    print(f"- validation: {'ok' if validation['ok'] else 'failed'}")
    print()
    print_table([
        {
            "id": case["id"],
            "status": "ready",
            "good": "-",
            "precision": "-",
            "sector_precision": "-",
            "unmatched": "-",
            "noise": "dry-run",
        }
        for case in cases
    ])


def print_table(rows):
    headers = ["id", "status", "good", "precision", "sector_precision", "unmatched", "noise"]
    widths = {header: len(header) for header in headers}
    for row in rows:
        for header in headers:
            widths[header] = max(widths[header], len(str(row.get(header, ""))))

    print(" | ".join(header.ljust(widths[header]) for header in headers))
    print("-+-".join("-" * widths[header] for header in headers))
    for row in rows:
        print(" | ".join(str(row.get(header, "")).ljust(widths[header]) for header in headers))


def parse_args():
    parser = argparse.ArgumentParser(
        description="Evaluate Juringle /analyze results against eval_cases.json."
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate eval data without calling /analyze.")
    parser.add_argument("--case-id", help="Run a single eval case.")
    parser.add_argument("--limit", type=int, help="Run the first N selected cases.")
    parser.add_argument("--output", help="Write evaluation report JSON to this path.")
    return parser.parse_args()


def main():
    args = parse_args()
    cases = load_json(EVAL_CASES_PATH)
    stock_db = load_json(STOCK_DB_PATH)
    candidate_db = load_json(STOCK_CANDIDATE_DB_PATH)
    maps = build_name_maps(stock_db, candidate_db)
    validation = validate_cases(cases, maps)
    selected_cases = select_cases(cases, args.case_id, args.limit)

    should_execute = not args.dry_run and (args.case_id or args.limit)
    if not should_execute:
        print_dry_run_summary(selected_cases, validation)
        if args.output:
            report = {
                "mode": "dry-run",
                "validation": validation,
                "case_ids": [case["id"] for case in selected_cases],
            }
            Path(args.output).write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return 0 if validation["ok"] else 1

    rows = []
    report_cases = []
    for index, case in enumerate(selected_cases):
        status_code, result, error = run_analyze(case, index)
        metrics = evaluate_case(case, result, status_code, error, maps)
        report_cases.append(metrics)
        rows.append({
            "id": metrics["id"],
            "status": metrics["status_code"],
            "good": ", ".join(metrics["good"]) or "-",
            "precision": metrics["good_precision_estimate"],
            "sector_precision": metrics["expected_sector_precision"],
            "unmatched": ", ".join(metrics["unmatched_good_candidates"]) or "-",
            "noise": metrics["noise_type"],
        })

    print_table(rows)
    if args.output:
        report = {
            "mode": "run",
            "validation": validation,
            "results": report_cases,
        }
        Path(args.output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
