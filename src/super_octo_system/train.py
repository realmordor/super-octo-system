from zeep import Client
import dotenv
import os
import datetime as dt

from zeep.plugins import HistoryPlugin
from zeep.settings import Settings
from zeep import xsd


dotenv.load_dotenv()
token = os.environ.get("DARWIN_LITE_TOKEN")

crs_codes = {
    "HDW": "Hadley Wood",
    "MOG": "Moorgate",
    "OLD": "Old Street",
    "EXR": "Essex Road",
    "HHY": "Highbury & Islington",
    "DYP": "Drayton Park",
    "FPK": "Finsbury Park",
    "HGY": "Harringay",
    "HRN": "Hornsey",
    "AAP": "Alexandra Palace",
    "NSG": "New Southgate",
    "OKL": "Oakleigh Park",
    "NBA": "New Barnet",
    "PBR": "Potters Bar",
    "BPK": "Brookmans Park",
    "WMG": "Welham Green",
    "HAT": "Hatfield",
    "WGC": "Welwyn Garden City",
}


class TrainLine:
    def __init__(self, stops: dict[str, str]):
        self.stops = stops


our_train = TrainLine(crs_codes)


def get_departure_board(
    crs_code: str, token: str, num_rows: int | None = 10, filter_crs: str | None = None
) -> tuple[str, dt.datetime, list]:
    WSDL = "http://lite.realtime.nationalrail.co.uk/OpenLDBWS/wsdl.aspx?ver=2021-11-01"
    settings = Settings(strict=False)
    history = HistoryPlugin()
    client = Client(wsdl=WSDL, settings=settings, plugins=[history])

    header = xsd.Element(
        "{http://thalesgroup.com/RTTI/2013-11-28/Token/types}AccessToken",
        xsd.ComplexType(
            [
                xsd.Element(
                    "{http://thalesgroup.com/RTTI/2013-11-28/Token/types}TokenValue",
                    xsd.String(),
                ),
            ]
        ),
    )
    header_value = header(TokenValue=token)

    try:
        if filter_crs:
            res = client.service.GetDepartureBoard(
                numRows=num_rows,
                crs=crs_code,
                filterCrs=filter_crs,
                _soapheaders=[header_value],
            )
        else:
            res = client.service.GetDepartureBoard(
                numRows=num_rows, crs=crs_code, _soapheaders=[header_value]
            )
    except Exception as e:
        print(f"Error fetching departure board: {e}")
        return ("", dt.datetime.now(), [])

    services = res.trainServices.service
    location_name = res.locationName
    generated = res.generatedAt
    return (location_name, generated, services)


def print_train_info(services: list, location_name: str, timestamp: dt.datetime):
    train_text = (
        f"Trains from {location_name}\nTime Now: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n"
        + f"{"=" * 29}\n"
        + "\n".join(
            [
                f"{t.std} to {t.destination.location[0].locationName}\nPlatform: {t.platform if t.platform else ''} - {t.etd}\n"
                for t in services
            ]
        )
    )
    return train_text
