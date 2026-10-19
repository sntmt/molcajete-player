# Revision de Codigo: Hallazgos y Resolucion

Revision del codigo de la rama `develop` realizada el 2026-10-19. Todos los hallazgos se corrigieron en la misma rama. Para cada uno se indica el problema original, la solucion aplicada y las pruebas que la cubren.

Estado de la bateria de pruebas: **29 pruebas antes de la revision, 97 despues**, todas en verde (`PYTHONPATH=src python3 -m unittest discover -s tests -v`). `ruff check src tests examples` sin avisos. La simulacion `examples/simulate_mux_s3_switch.py` pasa en modo `fakesink` y con ventana grafica (X11).

---

## 1. Errores

| # | Problema | Solucion | Pruebas |
| :--- | :--- | :--- | :--- |
| 1.1 | Un elemento sin URI detenia toda la lista, incluso con `loop: true`: `next()` devolvia `False` tanto al final de la lista como cuando el elemento no podia iniciarse, y `_handle_eos`/`_handle_error` llamaban a `stop()` en ambos casos. | Nuevo `_next_index()` que solo devuelve `None` al final de una lista sin bucle. `_handle_eos`/`_handle_error` usan `_next_index()` + `play_item_at_index()`; si el elemento falla, `_schedule_error()` lo delega en `_handle_error`, que lo salta. Un error programado que llega tras `stop()` se ignora (`_stopped`). | `test_player.py`: `test_item_without_uri_is_skipped_in_looping_playlist`, `test_end_of_non_looping_playlist_stops_and_calls_on_eos`, `test_scheduled_error_is_ignored_after_stop` |
| 1.2 | `"loop": "false"` activaba el bucle (`bool("false")` es `True`). | `parse_bool()` en `models.py`: acepta `true`/`false` y sus cadenas; cualquier otro valor lanza `ValueError` (salida `1` en la CLI). | `test_models.py`: `test_loop_accepts_boolean_strings`, `test_loop_rejects_non_boolean_values`; `test_cli.py`: `test_main_exit_code_1_when_json_invalid` |
| 1.3 | Las pruebas borraban la cache real del desarrollador al invocar `run.sh --help`. | `run.sh` ya no borra nada por si mismo, y todas las pruebas de `test_cli.py` fijan `MOLCAJETE_CACHE_DIR` a un directorio temporal. | `test_cli.py`: `test_run_sh_does_not_delete_cache_dir` |
| 1.4 | `run.sh` ejecutaba `rm -rf` sobre cualquier `--cache-dir` en cada invocacion. | Eliminado el bloque `rm -rf`. La limpieza es la opcion `--clean-cache` de la CLI (`MediaDownloader.clear_cache()`), que solo borra archivos con el patron de nombres del reproductor y conserva el directorio. `./run.sh` sin argumentos la activa para la demo. | `test_cli.py`: `test_run_sh_does_not_delete_cache_dir`, `test_main_clean_cache_removes_only_player_files`; `test_downloader.py`: `test_clear_cache_keeps_unrelated_files` |

## 2. Seguridad

| # | Problema | Solucion | Pruebas |
| :--- | :--- | :--- | :--- |
| 2.1 | Las redirecciones HTTP podian degradar `https://` a `http://`. | `_ValidatingRedirectHandler` valida cada salto con `validate_download_url()` y rechaza cualquier paso de `https` a otro esquema (`UnsafeRedirectError`), cancelando la descarga sin reintentar. Las peticiones pasan por `MediaDownloader._open()`. | `test_downloader.py`: `TestRedirectValidation` |
| 2.2 | `remote_url` (con tokens firmados) se registraba completa en los logs. | `play_item_at_index` usa `redact_url_for_log()`; los mensajes de error de GStreamer pasan por el nuevo `redact_urls_in_text()`. | `test_player.py`: `test_play_logs_redacted_remote_url`, `test_gstreamer_error_message_redacts_signed_urls`; `test_models.py`: `test_redact_urls_in_text` |
| 2.3 | `local_path` aceptaba rutas absolutas y enlaces simbolicos fuera de cualquier directorio. | `MediaItem.confine_local_path()` / `Playlist.confine_local_paths()` resuelven la ruta (incluidos enlaces) y exigen que quede dentro de `media_root`. CLI: `--media-root DIR`; `load_playlist_from_file(path, media_root=...)`. Sin `--media-root` se mantiene el comportamiento anterior (JSON de confianza), documentado en el README. | `test_models.py`: `TestLocalPathConfinement`; `test_loader.py`: `test_load_from_file_confines_local_paths_to_media_root`; `test_cli.py`: `test_main_exit_code_1_when_local_path_outside_media_root` |
| 2.4 | Sin limite de tamano, sin comprobar espacio libre y cache sin expulsion. | `max_bytes` (8 GiB) comprobado contra `Content-Length` y durante la descarga; `min_free_bytes` (256 MiB) con `shutil.disk_usage`. `prune_cache(items)` en `MolcajetePlayer.run()` elimina renditions ajenos a la lista y `.part` huerfanos (`--no-prune-cache` lo desactiva). | `test_downloader.py`: `test_rejects_download_larger_than_max_bytes_*`, `test_rejects_download_without_enough_free_space`, `test_prune_cache_removes_only_stale_player_files`; `test_player.py`: `test_run_resumes_downloader_and_prunes_cache`, `test_run_skips_pruning_when_disabled` |

## 3. Robustez y comportamiento

| # | Problema | Solucion | Pruebas |
| :--- | :--- | :--- | :--- |
| 3.1 | El SHA-256 completo se recalculaba en el hilo principal en cada vuelta del bucle. | `MediaDownloader.is_cached_and_valid()` memoriza la firma `(st_size, st_mtime_ns)` de cada archivo verificado (tambien al terminar una descarga) y solo vuelve a hashear si cambia. | `test_downloader.py`: `test_is_cached_and_valid_rehashes_only_when_file_changes`, `test_successful_download_is_verified_and_remembered`; `test_player.py`: el test de conmutacion comprueba que no se rehashea tras la descarga |
| 3.2 | El descargador no podia reanudarse tras `stop()`. | `MediaDownloader.resume()`, invocado por `MolcajetePlayer.play()` y `run()`. `stop(join_timeout)` permite esperar a los hilos. | `test_downloader.py`: `test_resume_allows_downloads_after_stop`; `test_player.py`: `test_play_resumes_from_paused_without_reloading_uri` |
| 3.3 | `on_download_complete` se ejecutaba en el hilo de descarga. | Nuevo parametro `on_download_complete` de `MolcajetePlayer`, encadenado al del descargador y reenviado con `GLib.idle_add`. El callback del descargador conserva su semantica (hilo de descarga) y queda documentada. | `test_player.py`: `test_player_download_callback_runs_on_main_loop` |
| 3.4 | El buffering nunca pausaba y reanudaba una pausa manual. | `_handle_buffering()`: pasa a `PAUSED` por debajo del 100 % y a `PLAYING` al 100 % solo si el estado objetivo (`_target_state`) es `PLAYING`; se ignora en streams en directo (`NO_PREROLL`). | `test_player.py`: `test_buffering_pauses_and_resumes`, `test_buffering_does_not_override_manual_pause`, `test_buffering_is_ignored_for_live_streams` |
| 3.5 | En Wayland quedaba una ventana Gtk vacia. | `_is_x11_display()` comprueba `GdkX11.X11Display` antes de elegir la ruta de overlay. Si aun asi no se obtiene el `XID`, `_fallback_to_autovideosink()` destruye la ventana y cambia a `autovideosink`. | `test_player.py`: `test_overlay_sink_not_used_outside_x11` |
| 3.6 | `stop()` reentraba desde la senal `destroy` de la ventana. | `stop()` desvincula `self._window` antes de llamar a `destroy()`. | `test_player.py`: `test_stop_is_safe_when_window_destroy_reenters` |
| 3.7 | `resolve_target_path` sobrescribia el `local_path` del JSON. | La ruta de cache se guarda en el nuevo atributo `MediaItem.cache_path` (no serializado). `get_effective_uri()` prueba `local_path`, luego `cache_path`, luego `remote_url`. Un `local_path` existente evita la descarga. | `test_downloader.py`: `test_preinstalled_local_path_skips_download_and_is_not_overwritten`; `test_models.py`: `TestEffectiveUriPriority` |

## 4. Validacion de datos

| Problema | Solucion | Pruebas |
| :--- | :--- | :--- |
| Elementos que no son objetos JSON | `MediaItem.from_dict` exige `dict`. | `test_rejects_non_object_items` |
| IDs duplicados | `Playlist.from_dict` los rechaza. | `test_rejects_duplicate_ids` |
| `metadata` no objeto / `title` o `id` `null` | `ValueError` en ambos casos (`metadata: null` se normaliza a `{}`). | `TestMediaItemValidation` |
| Elementos con solo `s3_url` | Se aceptan y el reproductor registra una advertencia al iniciar; mientras no se descarguen se saltan (ver 1.1). | `test_has_immediate_source`, `test_item_without_uri_is_skipped_in_looping_playlist` |
| `load_playlist_from_json` ambiguo | Nuevas `load_playlist_from_file()` (lanza `FileNotFoundError`) y `load_playlist_from_string()`. La original se conserva por compatibilidad. | `test_loader.py` |

## 5. Refactorizaciones

| # | Cambio |
| :--- | :--- |
| 1 | `_set_element_window_handle` usa `GstVideo.VideoOverlay.set_window_handle` cuando esta el typelib `GstVideo` (`gir1.2-gst-plugins-base-1.0`, anadido a las dependencias del README). `ctypes` solo se carga como respaldo si falta. |
| 2 | La limpieza de variables Snap se movio a `molcajete_player/env.py` (`sanitize_snap_env`) y se invoca en `main.py` y en el script de simulacion antes de importar `gi`. Importar `molcajete_player.player` ya no modifica `os.environ` (`test_env.py`: `test_importing_player_does_not_modify_environment`). |
| 3 | `MediaDownloader.discard_cached()` sustituye el uso externo de `_cleanup_part()` (renombrado a `_remove_file()`). |
| 4 | La comprobacion "existe + verificar + descartar" esta centralizada en `MediaDownloader.prepare()`, usada por el reproductor y por `ensure_downloading()`. |
| 5 | `_schedule_error()` reemplaza el patron duplicado `if GLib: GLib.idle_add(...) else: ...`. |
| 6 | Eliminada la dependencia `pydantic` (no se usaba) de `pyproject.toml` y `requirements.txt`. |
| 7 | `requirements.txt` documenta como instalar PyGObject (paquete del sistema o cabeceras de compilacion). |
| 8 | CLI: `--loop/--no-loop` (`BooleanOptionalAction`), `--clean-cache`, `--no-prune-cache`, `--media-root`; el codigo de salida `3` registra el *traceback* con `logging.exception`. |
| 9 | `env.SNAP_POLLUTED_VARS` incluye `LD_LIBRARY_PATH`, igual que `run.sh`; una prueba (`test_run_sh_and_python_sanitize_the_same_variables`) verifica que ambas listas no diverjan. |
| 10 | Configuracion `[tool.ruff]` y `[tool.pytest.ini_options]` en `pyproject.toml`, y flujo de GitHub Actions `.github/workflows/ci.yml` (ruff + pruebas con GStreamer del sistema). |

## 6. Seguimiento (2026-10-19, tras pruebas manuales)

| Problema reportado | Causa medida | Solucion | Pruebas |
| :--- | :--- | :--- | :--- |
| Los videos se congelaban al final | Al recibir `EOS`, la tuberia pasaba a `READY`, cargaba la nueva URI y prerrolaba desde cero: el ultimo fotograma quedaba congelado ~0,7 s (y ~1,7 s en el clip 3, que nunca se cacheaba porque su `sha256` de ejemplo ya no coincidia con el archivo servido). Ademas `buffer-size=64KB`/`buffer-duration=150ms` generaban cientos de mensajes de buffering. | Pre-buffer con `about-to-finish`: la siguiente URI se carga mientras el video actual sigue en pantalla y el cambio se confirma con `STREAM_START`. Descarga anticipada del siguiente `s3_url` (`--no-prefetch` la desactiva). Se eliminan los ajustes de bufer (valores por defecto de `playbin`) y se ignora el buffering del elemento precargado. Medido: 0 estancamientos en 6 cambios consecutivos. | `test_player.py`: `TestGaplessPreBuffer` |
| El audio seguia sonando tras cerrar | No reproducible con el codigo actual (Ctrl+C simple y doble, `SIGTERM`, `SIGHUP` y cierre de ventana terminan en ~250 ms sin flujo de audio residual). Se reforzo el cierre igualmente. | `SIGINT`/`SIGTERM`/`SIGHUP` se atienden con `GLib.unix_signal_add` y llaman a `stop()`; `stop()` pasa la tuberia a `NULL` antes que nada. | `test_player.py`: `TestShutdown` |
| Quitar el audio de los videos siempre | Quitar solo el flag de audio de `playbin` no bastaba: con HLS seguia abriendose un flujo de PulseAudio. | Audio desactivado sin opcion de activarlo: flag de audio eliminado, `fakesink` como `audio-sink` y `mute`. Verificado: 0 flujos de audio con `--sink auto` y `fakesink`. | `test_player.py`: `TestAudioAlwaysDisabled`; `test_cli.py`: `test_audio_option_does_not_exist` |
| Videos de ejemplo poco serios y con aspect ratio mixto | La lista mezclaba animaciones (conejo, Big Buck Bunny) en 16:9 y 4:3. | `sample_playlist.json`: imagen real 16:9 (flor 540p, Jellyfish 720p y 1080p). `test_stream.json`: streams de prueba de Mux (`test-streams.mux.dev`) y BipBop 16:9 de Apple. | `test_loader.py` |

Estado tras el seguimiento: 112 pruebas en verde, `ruff` sin avisos.

## 7. Limitaciones que permanecen

- La primera verificacion SHA-256 de un archivo ya presente en cache (por ejemplo, tras reiniciar el reproductor) sigue ejecutandose en el hilo principal. Las siguientes reutilizan la firma memorizada.
- Sin `--media-root`, `local_path` admite rutas absolutas: el JSON debe ser de confianza.
- `prune_cache` asume que el directorio de cache pertenece a una sola lista; si varias listas lo comparten, usa `--no-prune-cache`.
- La ruta de overlay X11 con respaldo `ctypes` no se puede probar sin servidor grafico; se verifico manualmente con la simulacion en X11.
- El arranque del primer elemento por red sigue teniendo el estancamiento propio del bufer inicial (0,4-1,9 s en las pruebas; hasta 4,8 s con BipBop de Apple).
- Un rendition que termina de descargarse en los ultimos segundos de un video se usa en la vuelta siguiente, porque la URI se elige en `about-to-finish`.
- El `sha256` de `jellyfish-*` y `flower-540p` corresponde al contenido servido el 2026-10-19; si esos servidores de terceros cambian el archivo, la descarga se descartara y el elemento se reproducira siempre por red.
