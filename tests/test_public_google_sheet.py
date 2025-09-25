import pandas as pd
from super_octo_system.public_google_sheet import get_public_google_sheet_as_dataframe


def test_get_public_google_sheet_as_dataframe():
    sheet_id = "1qMt1jKFf3OVILmA-MsQ8Ga-8vsYLsCX0ky00zairf9M"
    sheet_name = "thisWeekMenuMaker"
    df = get_public_google_sheet_as_dataframe(sheet_id, sheet_name)
    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    # Optionally check for expected columns, e.g.:
    # assert "ColumnName" in df.columns
