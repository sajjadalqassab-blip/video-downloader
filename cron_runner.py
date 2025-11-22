import json
from main import read_sheet_rows, process_one_url, write_result_to_sheet, sanitize_filename

def run():
    rows = read_sheet_rows()
    results = []

    for r in rows:
        row_index = r["row_index"]
        base_name = r["name"]
        urls = r["urls"]

        row_success = True
        drive_links_collected = []
        items = []

        for idx, url in enumerate(urls, start=1):
            per_link_name = base_name if idx == 1 else f"{base_name} ({idx})"

            try:
                uploaded, final_name = process_one_url(url, per_link_name)
                drive_url = uploaded.get("webViewLink") or ""
                drive_links_collected.append(drive_url)

                items.append({
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
                row_success = False
                items.append({
                    "url": url,
                    "filename": sanitize_filename(per_link_name),
                    "success": False,
                    "error": str(e)
                })

        # ✅ mark DONE only if ALL links succeeded
        if row_success:
            write_result_to_sheet(row_index, "\n".join(drive_links_collected), "DONE")
            status = "DONE"
        else:
            write_result_to_sheet(row_index, "\n".join(drive_links_collected), "PARTIAL")
            status = "PARTIAL"

        results.append({
            "row_index": row_index,
            "name": base_name,
            "status": status,
            "links_count": len(urls),
            "drive_links": drive_links_collected,
            "items": items
        })

    print(json.dumps({
        "success": True,
        "count": len(results),
        "results": results
    }, ensure_ascii=False))

if __name__ == "__main__":
    run()
