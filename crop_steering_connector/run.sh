#!/command/with-contenv sh
# with-contenv hands the container environment (SUPERVISOR_TOKEN) to the process S6 starts as the CMD.
exec python3 -I -B /app/start.py
