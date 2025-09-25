import requests
import os
import dotenv

dotenv.load_dotenv()


def get_met_office_forecast(
    lat, lon, timesteps="hourly", exclude_metadata="FALSE", include_location="TRUE"
):
    """
    Calls the Met Office site-specific API and returns the forecast JSON for the given location.
    """
    base_url = "https://data.hub.api.metoffice.gov.uk/sitespecific/v0/point/"
    url = base_url + timesteps
    api_key = os.getenv("MET_OFFICE_API_KEY", "")
    if not api_key:
        raise ValueError("MET_OFFICE_API_KEY environment variable not set.")
    headers = {"accept": "application/json", "apikey": api_key}
    params = {
        "excludeParameterMetadata": exclude_metadata,
        "includeLocationName": include_location,
        "latitude": lat,
        "longitude": lon,
    }
    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    # Example usage: Hadley Wood
    lat = 51.6512
    lon = -0.1442
    try:
        forecast = get_met_office_forecast(lat, lon)
        print(forecast)
    except requests.exceptions.HTTPError as e:
        print(f"HTTP error: {e}")
