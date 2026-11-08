# FE 가이드 준수 측정용 Playwright 러너 (브라우저는 이미지에 포함, playwright 패키지만 같은 버전으로 설치)
FROM mcr.microsoft.com/playwright:v1.58.2-noble
ENV TZ=Asia/Seoul NODE_PATH=/opt/pw/node_modules
WORKDIR /opt/pw
RUN npm install --no-audit --no-fund --no-package-lock playwright@1.58.2 \
 && npm cache clean --force
WORKDIR /qa
CMD ["node", "/qa/fe_guide_check.js"]
