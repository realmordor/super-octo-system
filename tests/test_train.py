import os
from super_octo_system import train


def test_placeholder():
    # TODO: Add real tests for train.py
    assert True


def test_get_departure_board_api():
    token = os.environ.get("DARWIN_LITE_TOKEN")
    assert token, "DARWIN_LITE_TOKEN must be set in the environment"
    crs_code = "MOG"  # Moorgate, a valid CRS code
    location_name, generated, services = train.get_departure_board(crs_code, token)
    assert isinstance(location_name, str)
    assert location_name != ""
    assert services is not None


def test_test():
    from zeep import Client, Settings, xsd
    from zeep.plugins import HistoryPlugin

    LDB_TOKEN = "1e12ffb5-f28d-4124-8017-6416ba6f0471"
    WSDL = "http://lite.realtime.nationalrail.co.uk/OpenLDBWS/wsdl.aspx?ver=2021-11-01"

    if LDB_TOKEN == "":
        raise Exception(
            "Please configure your OpenLDBWS token in getDepartureBoardExample!"
        )

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
    header_value = header(TokenValue=LDB_TOKEN)

    res = client.service.GetDepartureBoard(
        numRows=10, crs="EUS", _soapheaders=[header_value]
    )

    print("Trains at " + res.locationName)
    print(
        "==============================================================================="
    )

    services = res.trainServices.service

    i = 0
    while i < len(services):
        t = services[i]
        print(t.std + " to " + t.destination.location[0].locationName + " - " + t.etd)
        i += 1
