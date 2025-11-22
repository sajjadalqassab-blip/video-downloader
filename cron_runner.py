import json
from main import read_sheet_rows, process_one_url

def run():
    rows = read_sheet_rows()
    results = []

    for r in rows:
        url = r["url"]
        name = r["name"]

        try:
            uploaded, final_name = process_one_url(url, name)
            results.append({
                "row_index": r["row_index"],
                "url": url,
                "filename": final_name,
                "success": True,
                "drive_file": {
                    "id": uploaded.get("id"),
                    "webViewLink": uploaded.get("webViewLink"),
                    "webContentLink": uploaded.get("webContentLink"),
                }
            })
        except Exception as e:
            results.append({
                "row_index": r["row_index"],
                "url": url,
                "filename": name,
                "success": False,
                "error": str(e)
            })

    print(json.dumps({
        "success": True,
        "count": len(results),
        "results": results
    }, ensure_ascii=False))

if __name__ == "__main__":
    run()
