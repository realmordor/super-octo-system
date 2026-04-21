import pandas as pd


def get_public_google_sheet_as_dataframe_by_gid(
    sheet_id: str, gid: str
) -> pd.DataFrame:
    """
    Fetches a public Google Sheet by ID and GID, returns it as a pandas DataFrame.

    Args:
        sheet_id (str): The ID of the Google Sheet (from the URL).
        gid (str): The GID of the specific sheet/tab.

    Returns:
        pd.DataFrame: DataFrame containing the sheet data.
    """
    # Use GID-based URL for direct sheet access
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    )
    df = pd.read_csv(url, skiprows=3)
    return df


def get_recipe_names(
    sheet_id: str = "1qMt1jKFf3OVILmA-MsQ8Ga-8vsYLsCX0ky00zairf9M",
    gid: str = "802866866",
) -> list:
    """
    Extracts just the recipe names from the first column of the ingredients sheet.

    Args:
        sheet_id (str): The ID of the Google Sheet.
        gid (str): The GID of the ingredients sheet.

    Returns:
        list: List of recipe names.
    """
    # Get the ingredients dataframe
    df_ingredients = get_public_google_sheet_as_dataframe_by_gid(sheet_id, gid)

    # Get the first column (assuming it contains recipe names)
    first_column = df_ingredients.iloc[:, 0]

    # Extract non-empty, non-null values that are likely recipe names
    recipe_names = []
    for value in first_column:
        if pd.notna(value) and str(value).strip() != "":
            recipe_names.append(str(value).strip())

    return recipe_names


def get_recipes_by_ingredient(ingredient: str) -> dict:
    """
    Takes an ingredient as input and returns which recipes use that ingredient with amounts.

    Args:
        ingredient (str): The ingredient to search for in recipes.

    Returns:
        dict: Dictionary with recipe names as keys and ingredient amounts as values.
    """
    sheet_id = "1qMt1jKFf3OVILmA-MsQ8Ga-8vsYLsCX0ky00zairf9M"
    gid = "802866866"

    # Get the ingredients dataframe using GID
    df_ingredients = get_public_google_sheet_as_dataframe_by_gid(sheet_id, gid)

    # Convert ingredient to lowercase for case-insensitive matching
    ingredient_lower = ingredient.lower()

    # Track current recipe name and matching recipes with amounts
    current_recipe = None
    matching_recipes = {}

    for index, row in df_ingredients.iterrows():
        # Check if first column has a recipe name (non-empty)
        first_col_value = row.iloc[0]
        if pd.notna(first_col_value) and str(first_col_value).strip() != "":
            current_recipe = str(first_col_value).strip()

        # Check all columns for the ingredient
        ingredient_found = False
        for column in df_ingredients.columns:
            cell_value = str(row[column]).lower() if pd.notna(row[column]) else ""
            if ingredient_lower in cell_value and current_recipe:
                ingredient_found = True
                break

        if (
            ingredient_found
            and current_recipe
            and current_recipe not in matching_recipes
        ):
            # Get amount and unit for this ingredient
            amount = ""
            unit = ""

            # Look for 'amount' and 'unit' columns (case-insensitive)
            for col in df_ingredients.columns:
                col_lower = str(col).lower()
                if "amount" in col_lower:
                    amount_val = row[col]
                    if pd.notna(amount_val):
                        amount = str(amount_val).strip()
                elif "unit" in col_lower:
                    unit_val = row[col]
                    if pd.notna(unit_val):
                        unit = str(unit_val).strip()

            # Combine amount and unit
            if amount and unit:
                amount_text = f"{amount} {unit}"
            elif amount:
                amount_text = amount
            elif unit:
                amount_text = unit
            else:
                amount_text = "Amount not specified"

            matching_recipes[current_recipe] = amount_text

    return matching_recipes


if __name__ == "__main__":
    # Test getting all recipe names
    recipes = get_recipe_names()
    print("All recipe names:")
    for recipe in recipes:
        print(f"- {recipe}")

    # Test searching by ingredient
    ingredient_to_search = "onion"
    matching_recipes = get_recipes_by_ingredient(ingredient_to_search)
    print(f"\nRecipes containing '{ingredient_to_search}':")
    for recipe, amount in matching_recipes.items():
        print(f"- {recipe}: {amount}")
