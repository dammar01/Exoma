def quantity(value):
    """Clamp negative quantities to zero."""
    return max(0, value)


def total(price, units):
    """Charge the unit price for each unit."""
    return price * units

