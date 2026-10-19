# Reglas del Proyecto (Molcajete Player)

## Flujo de Desarrollo Obligatorio
- **Pruebas y Documentación en Cada Cambio**: Todo cambio en el código fuente (nuevas funcionalidades, correcciones de errores, refactorizaciones o cambios en la CLI/modelos) **debe ir siempre acompañado** de:
  1. **Actualización o creación de tests unitarios** en `tests/` que verifiquen el nuevo comportamiento y la ejecución de la batería de pruebas (`PYTHONPATH=src python3 -m unittest discover -s tests -v`) para garantizar que todas pasen.
  2. **Actualización de la documentación** en `README.md` (y docstrings correspondientes) para mantenerla sincronizada con la implementación actual.
