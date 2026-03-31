"""
rentcast_client.py
------------------
Thin wrapper around the RentCast AVM (Automated Valuation Model) API.
Docs: https://developers.rentcast.io/reference/value-estimate

Requires:
    RENTCAST_API_KEY environment variable (or set in .env)

Usage as a module:
    from rentcast_client import get_property_value
    price = get_property_value("4203 Kings Canyon Drive, Taylor, TX 76574")

Usage as a CLI:
    python rentcast_client.py "4203 Kings Canyon Drive, Taylor, TX 76574"
    python rentcast_client.py --address "4203 Kings Canyon Drive, Taylor, TX 76574" --property-type "Single Family"
"""

import argparse
import json
import logging
import os
import sys
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

RENTCAST_BASE_URL = "https://api.rentcast.io/v1"


def get_property_value(
    address: str,
    property_type: str = "Single Family",
    bedrooms: Optional[int] = None,
    bathrooms: Optional[float] = None,
    square_footage: Optional[int] = None,
    api_key: Optional[str] = None,
) -> Optional[float]:
    """
    Call the RentCast /avm/value endpoint and return the estimated price.

    Args:
        address:        Full property address (street, city, state, zip).
        property_type:  "Single Family", "Condo", "Townhouse", etc.
        bedrooms:       Optional — improves accuracy.
        bathrooms:      Optional — improves accuracy.
        square_footage: Optional — improves accuracy.
        api_key:        Defaults to RENTCAST_API_KEY env var.

    Returns:
        Estimated price as a float, or None on failure.
    """
    key = api_key or os.getenv("RENTCAST_API_KEY")
    if not key:
        logger.error(
            "RENTCAST_API_KEY not set. Add it to your .env file or environment."
        )
        return None

    params: dict = {"address": address, "propertyType": property_type}
    if bedrooms is not None:
        params["bedrooms"] = bedrooms
    if bathrooms is not None:
        params["bathrooms"] = bathrooms
    if square_footage is not None:
        params["squareFootage"] = square_footage

    try:
        response = requests.get(
            f"{RENTCAST_BASE_URL}/avm/value",
            headers={"X-Api-Key": key, "accept": "application/json"},
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        price = data.get("price")
        if price is None:
            logger.warning(f"RentCast returned no 'price' for: {address}")
            logger.debug(f"Full response: {json.dumps(data, indent=2)}")
            return None
        price_low = data.get("priceLow")
        price_high = data.get("priceHigh")
        low_str = f"${price_low:,.0f}" if isinstance(price_low, (int, float)) else "?"
        high_str = f"${price_high:,.0f}" if isinstance(price_high, (int, float)) else "?"
        logger.info(f"RentCast AVM: {address} → ${price:,.0f} (range: {low_str} – {high_str})")
        return float(price)

    except requests.exceptions.HTTPError as e:
        logger.error(f"RentCast HTTP error for '{address}': {e} — {response.text}")
    except requests.exceptions.RequestException as e:
        logger.error(f"RentCast request failed for '{address}': {e}")

    return None


def get_property_details(
    address: str,
    api_key: Optional[str] = None,
) -> Optional[dict]:
    """
    Call the RentCast /properties endpoint and return a flat dict of
    property characteristics: bedrooms, bathrooms, sqft, lot size,
    year built, property type, tax assessed value, last sale price, etc.

    Returns None if the address is not found or the API call fails.
    """
    key = api_key or os.getenv("RENTCAST_API_KEY")
    if not key:
        logger.error("RENTCAST_API_KEY not set.")
        return None

    try:
        response = requests.get(
            f"{RENTCAST_BASE_URL}/properties",
            headers={"X-Api-Key": key, "accept": "application/json"},
            params={"address": address},
            timeout=15,
        )
        response.raise_for_status()
        results = response.json()
        if not results:
            logger.warning(f"RentCast properties: no results for '{address}'")
            return None

        prop = results[0]  # best match

        # Latest tax assessed value
        tax_assessments = prop.get("taxAssessments") or {}
        latest_year = max(tax_assessments.keys(), default=None)
        tax_value = tax_assessments[latest_year]["value"] if latest_year else None

        return {
            "rc_bedrooms":       prop.get("bedrooms"),
            "rc_bathrooms":      prop.get("bathrooms"),
            "rc_square_feet":    prop.get("squareFootage"),
            "rc_lot_size_sqft":  prop.get("lotSize"),
            "rc_year_built":     prop.get("yearBuilt"),
            "rc_property_type":  prop.get("propertyType"),
            "rc_last_sale_price": prop.get("lastSalePrice"),
            "rc_last_sale_date":  (prop.get("lastSaleDate") or "")[:10],
            "rc_tax_assessed_value": tax_value,
            "rc_county":         prop.get("county"),
        }

    except requests.exceptions.HTTPError as e:
        logger.error(f"RentCast properties HTTP error for '{address}': {e} — {response.text}")
    except requests.exceptions.RequestException as e:
        logger.error(f"RentCast properties request failed for '{address}': {e}")

    return None


def get_property_value_detailed(
    address: str,
    property_type: str = "Single Family",
    bedrooms: Optional[int] = None,
    bathrooms: Optional[float] = None,
    square_footage: Optional[int] = None,
    api_key: Optional[str] = None,
) -> Optional[dict]:
    """
    Same as get_property_value() but returns the full API response dict.

    Keys include: price, priceLow, priceHigh, priceRangePercentage,
                  latitude, longitude, addressLine1, city, state, zipCode, etc.
    """
    key = api_key or os.getenv("RENTCAST_API_KEY")
    if not key:
        logger.error("RENTCAST_API_KEY not set.")
        return None

    params: dict = {"address": address, "propertyType": property_type}
    if bedrooms is not None:
        params["bedrooms"] = bedrooms
    if bathrooms is not None:
        params["bathrooms"] = bathrooms
    if square_footage is not None:
        params["squareFootage"] = square_footage

    try:
        response = requests.get(
            f"{RENTCAST_BASE_URL}/avm/value",
            headers={"X-Api-Key": key, "accept": "application/json"},
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    except requests.exceptions.HTTPError as e:
        logger.error(f"RentCast HTTP error for '{address}': {e} — {response.text}")
    except requests.exceptions.RequestException as e:
        logger.error(f"RentCast request failed for '{address}': {e}")

    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Look up a property value estimate via the RentCast AVM API."
    )
    parser.add_argument(
        "address",
        nargs="?",
        help="Full property address (e.g. '123 Main St, Austin, TX 78701')",
    )
    parser.add_argument("--address", dest="address_flag", metavar="ADDRESS",
                        help="Alternative flag for the address.")
    parser.add_argument("--property-type", default="Single Family",
                        help="Property type (default: 'Single Family')")
    parser.add_argument("--bedrooms", type=int, default=None)
    parser.add_argument("--bathrooms", type=float, default=None)
    parser.add_argument("--sqft", type=int, default=None, dest="square_footage")
    parser.add_argument("--full", action="store_true",
                        help="Print full API response JSON instead of just the price.")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    address = args.address or args.address_flag
    if not address:
        parser.error("Provide an address as a positional argument or via --address.")

    if args.full:
        result = get_property_value_detailed(
            address,
            property_type=args.property_type,
            bedrooms=args.bedrooms,
            bathrooms=args.bathrooms,
            square_footage=args.square_footage,
        )
        if result:
            print(json.dumps(result, indent=2))
        else:
            print("No result returned.", file=sys.stderr)
            sys.exit(1)
    else:
        price = get_property_value(
            address,
            property_type=args.property_type,
            bedrooms=args.bedrooms,
            bathrooms=args.bathrooms,
            square_footage=args.square_footage,
        )
        if price is not None:
            print(f"\nEstimated value: ${price:,.0f}")
        else:
            print("Could not retrieve a value estimate.", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
