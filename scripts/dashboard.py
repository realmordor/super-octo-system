import os
import streamlit as st
import pandas as pd
from super_octo_system.train import get_departure_board, crs_codes


def main():
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
    num_rows = st.slider("Number of rows", min_value=1, max_value=20, value=10)
    filter_crs = st.text_input("Filter by CRS code (optional)")

    if st.button("Get Departure Board"):
        location_name, generated, services = get_departure_board(
            crs_code, token, num_rows, filter_crs or None
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
        # Limit the number of rows after filtering
        df = pd.DataFrame(data).head(num_rows)
        st.subheader(f"Trains from {location_name} (as of {generated})")
        st.dataframe(df)


if __name__ == "__main__":
    main()
