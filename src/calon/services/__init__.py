"""The service layer: everything that has to touch both the database and the domain.

Services own transactions, translate rows into the domain's pure value objects, and write
the outcome back. The domain does not know they exist; the API does not know how they
work.
"""
