#!/bin/sh

# Allow this script to fail without failing a build
set +e

USER_GROUP=$(stat -c '%U:%G' "${APP_ROOT}")

mkdir -p ${APP_LOGS}
chown -R ${USER_GROUP} ${VENV_DIR}
chown -R ${APP_UID}:${APP_GID} ${APP_ROOT}
chown -R ${APP_UID}:${APP_GID} ${APP_LOGS}
find ${APP_ROOT} -type f -exec chmod 644 {} \;
find ${APP_ROOT} -type d -exec chmod 755 {} \;
find ${APP_LOGS} -type f -exec chmod 644 {} \;
find ${APP_LOGS} -type d -exec chmod 755 {} \;

# Always end successfully
exit 0
