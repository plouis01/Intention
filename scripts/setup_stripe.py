"""Setup Stripe: create Product + Prices and update .env.

Supports both test (sk_test_) and live (sk_live_) keys.

Usage:
    python scripts/setup_stripe.py sk_live_XXXX
    python scripts/setup_stripe.py sk_test_XXXX

Or if STRIPE_SECRET_KEY is already in .env:
    python scripts/setup_stripe.py
"""

import sys
from pathlib import Path

import stripe

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

PRODUCT_NAME = "Intention Pro"
PRODUCT_METADATA_KEY = "intention_product"
MONTHLY_PRICE_EUR = 8900  # 89€ in cents
YEARLY_PRICE_EUR = 84900  # 849€ in cents


def get_secret_key() -> str:
    """Get Stripe secret key from CLI arg or .env."""
    if len(sys.argv) > 1:
        return sys.argv[1]

    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("STRIPE_SECRET_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val.startswith(("sk_test_", "sk_live_")):
                    return val

    print("Usage: python scripts/setup_stripe.py sk_test_XXXX or sk_live_XXXX")
    print("Or set STRIPE_SECRET_KEY in .env first.")
    sys.exit(1)


def find_or_create_product() -> str:
    """Find existing product or create a new one. Returns product ID."""
    # Search by metadata
    products = stripe.Product.search(query=f"metadata['{PRODUCT_METADATA_KEY}']:'true'")
    if products.data:
        product = products.data[0]
        print(f"  Found existing product: {product.id} ({product.name})")
        return product.id

    product = stripe.Product.create(
        name=PRODUCT_NAME,
        description="Accès complet à l'analyse de l'intention du législateur",
        metadata={PRODUCT_METADATA_KEY: "true"},
    )
    print(f"  Created product: {product.id}")
    return product.id


def find_or_create_price(product_id: str, amount: int, interval: str) -> str:
    """Find existing price or create a new one. Returns price ID."""
    # List existing prices for this product
    prices = stripe.Price.list(product=product_id, active=True, limit=100)
    for price in prices.data:
        recurring = price.recurring
        if (
            recurring
            and recurring.interval == interval
            and price.unit_amount == amount
            and price.currency == "eur"
        ):
            label = "mensuel" if interval == "month" else "annuel"
            print(f"  Found existing {label} price: {price.id} ({amount / 100}€/{interval})")
            return price.id

    price = stripe.Price.create(
        product=product_id,
        unit_amount=amount,
        currency="eur",
        recurring={"interval": interval},
    )
    label = "mensuel" if interval == "month" else "annuel"
    print(f"  Created {label} price: {price.id} ({amount / 100}€/{interval})")
    return price.id


def update_env_file(secret_key: str, monthly_price_id: str, yearly_price_id: str) -> None:
    """Update .env file with Stripe variables."""
    stripe_vars = {
        "STRIPE_SECRET_KEY": secret_key,
        "STRIPE_WEBHOOK_SECRET": "",
        "STRIPE_PRO_MONTHLY_PRICE_ID": monthly_price_id,
        "STRIPE_PRO_YEARLY_PRICE_ID": yearly_price_id,
    }

    existing_lines = []
    if ENV_FILE.exists():
        existing_lines = ENV_FILE.read_text().splitlines()

    # Update existing vars or track which to append
    updated_keys = set()
    new_lines = []
    for line in existing_lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in stripe_vars:
            # Only overwrite if we have a non-empty value (don't overwrite webhook secret if already set)
            if stripe_vars[key] or key != "STRIPE_WEBHOOK_SECRET":
                new_lines.append(f"{key}={stripe_vars[key]}")
            else:
                new_lines.append(line)  # Keep existing value
            updated_keys.add(key)
        else:
            new_lines.append(line)

    # Append missing vars
    for key, value in stripe_vars.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(new_lines) + "\n")
    print(f"\n  Updated {ENV_FILE}")


def main() -> None:
    secret_key = get_secret_key()

    if not secret_key.startswith(("sk_test_", "sk_live_")):
        print(f"ERROR: Invalid key format: {secret_key[:12]}...")
        print("Key must start with sk_test_ or sk_live_")
        sys.exit(1)

    mode = "LIVE" if secret_key.startswith("sk_live_") else "TEST"
    if mode == "LIVE":
        print(f"\n⚠  Mode LIVE détecté — les objets seront créés sur le compte de production.")
        response = input("Continuer ? [y/N] ")
        if response.lower() != "y":
            sys.exit(1)

    stripe.api_key = secret_key

    print(f"\n1. Setting up Stripe {mode} Product...")
    product_id = find_or_create_product()

    print(f"\n2. Setting up Stripe {mode} Prices...")
    monthly_price_id = find_or_create_price(product_id, MONTHLY_PRICE_EUR, "month")
    yearly_price_id = find_or_create_price(product_id, YEARLY_PRICE_EUR, "year")

    print("\n3. Updating .env...")
    update_env_file(secret_key, monthly_price_id, yearly_price_id)

    print("\n--- Configuration complete ---")
    print(f"  STRIPE_SECRET_KEY={secret_key[:12]}...{secret_key[-4:]}")
    print(f"  STRIPE_PRO_MONTHLY_PRICE_ID={monthly_price_id}")
    print(f"  STRIPE_PRO_YEARLY_PRICE_ID={yearly_price_id}")
    print("\nRedémarrez le backend pour prendre en compte les changements.")
    if mode == "TEST":
        print("\nPour tester:")
        print("  1. Cliquez 'S'abonner' sur la page pricing")
        print("  2. Utilisez la carte test: 4242 4242 4242 4242")
        print("  3. Date: n'importe quelle date future, CVC: 123")
    else:
        print("\nMode LIVE actif — les paiements seront réels.")


if __name__ == "__main__":
    main()
