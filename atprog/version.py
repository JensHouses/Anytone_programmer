APP_NAME = "atprog"
APP_TITLE = "AnyTone AT-D878UV II Plus Programmer"
# Zaehlung: v1.00 ist der erste vollstaendige Stand; jede weitere Iteration
# zaehlt in Hundertstel-Schritten weiter (1.01, 1.02, ...). Bewusst getrennt von
# der Geraetefirmware (4.00) und von der Adressplan-Revision (r10).
__version__ = "1.00"
# Revision des mitgelieferten Adressplans (siehe atprog/layouts/).
# Bewusst eine eigene Zaehlung: sie hat nichts mit der Geraetefirmware (4.00)
# und nichts mit der Programmversion von atprog zu tun.
PLAN_REVISION = 10
LAYOUT_VERSION = "r%d" % PLAN_REVISION
