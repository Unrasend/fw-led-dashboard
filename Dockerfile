# The dashboard daemon alone, for `install.sh --docker`. The tray and the settings window stay
# on the desktop and reach it through the control socket; packaging/framework-led-dashboard-docker
# runs this image with the few things the daemon needs (see the comment at its top).
FROM alpine:3.22

ARG VERSION=dev
LABEL org.opencontainers.image.title="Framework LED Matrix dashboard" \
      org.opencontainers.image.source="https://github.com/Unrasend/fw-led-dashboard" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.licenses="MIT"

# Python runs the daemon (standard library only); PipeWire's pw-record captures the sound.
RUN apk add --no-cache python3 pipewire-tools

COPY src/framework_led_dashboard /opt/framework-led-dashboard/framework_led_dashboard
# Readable by whoever runs it, and compiled now: the root file system is read-only later.
RUN chmod -R a+rX /opt/framework-led-dashboard \
    && python3 -m compileall -q /opt/framework-led-dashboard

ENV PYTHONPATH=/opt/framework-led-dashboard \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# Nobody by default; the wrapper runs it as the desktop user, who owns the sockets and ports.
USER 65534:65534
ENTRYPOINT ["python3", "-P", "-m", "framework_led_dashboard"]
CMD ["--run", "/config/config.toml"]
