import pandas as pd
import urllib.parse


def get_latest_response_summary(sheet_id: str, sheet_name: str) -> pd.DataFrame:
    """
    Fetches a public Google Sheet and summarizes the latest response into a DataFrame.
    Args:
        sheet_id (str): The ID of the Google Sheet (from the URL).
        sheet_name (str): The name of the sheet/tab to fetch.
    Returns:
        pd.DataFrame: DataFrame summarizing the latest response.
    """
    # Use direct CSV export URL for the sheet
    base_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&sheet={urllib.parse.quote(sheet_name)}"
    df = pd.read_csv(base_url)
    if df.empty:
        return pd.DataFrame()
    # Assume the latest response is the last row
    latest = df.iloc[-1]

    # Organise columns by days of the week
    days = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]
    collated = {}
    for col in df.columns:
        for day in days:
            if day.lower() in col.lower():
                # Use the part before the day as the entry name
                entry_name = col.split(day)[0].strip().replace("_", " ")
                if entry_name not in collated:
                    collated[entry_name] = {"Entry": entry_name}
                collated[entry_name][day] = latest[col]
                break
    # Also include any columns that do not match a day
    for col in df.columns:
        if not any(day.lower() in col.lower() for day in days):
            collated[col] = latest[col]

    # Convert collated dict to DataFrame
    # If there are entries with days, make a DataFrame with entry names as rows and days as columns
    entries_with_days = {k: v for k, v in collated.items() if isinstance(v, dict)}
    if entries_with_days:
        summary_df = pd.DataFrame(entries_with_days.values())
        # Add any non-day columns as a separate row at the bottom
        non_day_entries = {k: v for k, v in collated.items() if not isinstance(v, dict)}
        if non_day_entries:
            non_day_df = pd.DataFrame([non_day_entries])
            summary_df = pd.concat(
                [summary_df, non_day_df], ignore_index=True, sort=False
            )
    else:
        summary_df = pd.DataFrame([collated])
    # Drop the 'Timestamp' column if present
    if "Timestamp" in summary_df.columns:
        summary_df = summary_df.drop(columns=["Timestamp"])
    # Remove any brackets from the 'Entry' column
    if "Entry" in summary_df.columns:
        summary_df["Entry"] = summary_df["Entry"].str.replace(
            r"[\[\]\(\)]", "", regex=True
        )
    # Remove the last row from the DataFrame
    if len(summary_df) > 1:
        summary_df = summary_df.iloc[:-1]
    return summary_df


# Example usage:
# df_summary = get_latest_response_summary(
#     "1AFQrHf15-Pzyvbmn9jzPU1FvXnW4u9VllsIWAQ0Mq6U",
#     "Form Responses 1"
# )
# print(df_summary)
