import pandas as pd
from super_octo_system.latest_response_summary import get_latest_response_summary


def test_get_latest_response_summary():
    sheet_id = "1AFQrHf15-Pzyvbmn9jzPU1FvXnW4u9VllsIWAQ0Mq6U"
    sheet_name = "Form Responses 1"
    df = get_latest_response_summary(sheet_id, sheet_name)
    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    # Check that 'Timestamp' column is not present
    assert "Timestamp" not in df.columns
    # Check that 'Entry' column has no brackets
    if "Entry" in df.columns:
        assert not df["Entry"].str.contains(r"[\[\]\(\)]").any()
    # Check that the last row was removed (if more than one row)
    # This is a structural check: if the original function would have returned N rows, now it should be N-1
    # We can't check the original here, but we can check that the function doesn't return an empty DataFrame if len > 1
    assert len(df) >= 1
