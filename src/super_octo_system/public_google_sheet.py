import pandas as pd
import urllib.parse


def get_public_google_sheet_as_dataframe(
    sheet_id: str, sheet_name: str
) -> pd.DataFrame:
    """
    Fetches a public Google Sheet by ID and sheet name, returns it as a pandas DataFrame.
    Args:
        sheet_id (str): The ID of the Google Sheet (from the URL).
        sheet_name (str): The name of the sheet/tab to fetch.
    Returns:
        pd.DataFrame: DataFrame containing the sheet data.
    """
    # Get the GID for the sheet name
    # This requires fetching the sheet metadata, but for most public sheets, the first sheet is gid=0
    # If you know the GID, you can use it directly. Otherwise, you can use sheet_name if it's the first tab.
    # For this example, we assume the tab is the first sheet (gid=0)
    # Use direct CSV export URL for the sheet
    base_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&sheet={urllib.parse.quote(sheet_name)}"
    df = pd.read_csv(base_url)
    return df.iloc[3:].dropna(axis=1, thresh=1).reset_index(drop=True)


# Example usage:
# df = get_public_google_sheet_as_dataframe(
#     "1qMt1jKFf3OVILmA-MsQ8Ga-8vsYLsCX0ky00zairf9M",
#     "thisWeekMenuMaker"
# )
# print(df)
