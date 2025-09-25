import datetime
import os
import streamlit as st
import pandas as pd
from super_octo_system.train import get_departure_board, crs_codes


def main():
    from super_octo_system.met_office import get_met_office_forecast

    # Hadley Wood coordinates
    hw_lat = 51.6512
    hw_lon = -0.1442
    try:
        forecast = get_met_office_forecast(hw_lat, hw_lon)
        st.subheader(
            "Met Office Forecast for Hadley Wood - Feels Like & Precipitation Table"
        )
        # Extract timeSeries data
        time_series = []
        if "features" in forecast and forecast["features"]:
            props = forecast["features"][0]["properties"]
            import pytz
            from dateutil import parser

            london_tz = pytz.timezone("Europe/London")
            today = datetime.datetime.now(london_tz).date()
            for entry in props.get("timeSeries", []):
                utc_time = parser.isoparse(entry.get("time"))
                local_time = utc_time.astimezone(london_tz)
                if local_time.date() != today:
                    continue
                temp = entry.get("feelsLikeTemperature")
                precip = entry.get("probOfPrecipitation")
                temp_rounded = round(temp) if temp is not None else None
                if precip is not None and precip >= 70:
                    precip_emoji = "🌧️"
                elif precip is not None and precip >= 30:
                    precip_emoji = "🌦️"
                else:
                    precip_emoji = "☀️"
                if temp_rounded is not None and temp_rounded <= 5:
                    temp_emoji = "🥶"
                elif temp_rounded is not None and temp_rounded >= 25:
                    temp_emoji = "🥵"
                else:
                    temp_emoji = "😊"
                time_series.append(
                    {
                        local_time.strftime("%H:%M"): {
                            "🌡️ Feels Like Temp (°C)": f"{temp_rounded} {temp_emoji}",
                            "🌧️ Probability of Precipitation (%)": f"{precip} {precip_emoji}",
                        }
                    }
                )
            if time_series:
                # Convert to dict of columns for transpose
                columns = {}
                for entry in time_series:
                    for k, v in entry.items():
                        columns[k] = v
                df_weather = pd.DataFrame(columns)
                st.dataframe(df_weather, use_container_width=True)
            else:
                st.write("No timeSeries data available.")
        else:
            st.write(forecast)
    except Exception as e:
        st.error(f"Met Office API error: {e}")
    st.title("National Rail Departure Board Dashboard")
    token = os.environ.get("DARWIN_LITE_TOKEN")
    if not token:
        st.error("DARWIN_LITE_TOKEN not set in environment.")
        return

    crs_code = st.selectbox(
        "Select Departure Station",
        options=list(crs_codes.keys()),
        format_func=lambda x: crs_codes[x],
    )
    dest_crs_code = st.selectbox(
        "Select Destination Station",
        options=list(crs_codes.keys()),
        format_func=lambda x: crs_codes[x],
        index=1,
    )
    generated = datetime.datetime.min
    if st.button("Get Departure Board") or (
        datetime.datetime.now() - generated > datetime.timedelta(seconds=5)
    ):
        location_name, generated, services = get_departure_board(
            crs_code, token, 10, None
        )
        if not services:
            st.warning("No train services found or error occurred.")
            return
        data = []
        for t in services:
            # Filter by destination CRS code if selected
            dest_code = getattr(t.destination.location[0], "crs", "")
            if dest_crs_code and dest_code != dest_crs_code:
                continue
            data.append(
                {
                    "Time": getattr(t, "std", ""),
                    "Destination": getattr(
                        t.destination.location[0], "locationName", ""
                    ),
                    "Platform": getattr(t, "platform", ""),
                    "ETD": getattr(t, "etd", ""),
                }
            )
        if not data:
            st.warning("No train services match the destination CRS code filter.")
            return
        df = pd.DataFrame(data)
        st.subheader(f"Trains from {location_name} (as of {generated})")
        st.dataframe(df)


if __name__ == "__main__":
    main()
