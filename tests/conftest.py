import os
import tempfile

# 파이프라인 모듈은 import 시점에 로그 파일 위치를 정한다 (log = get_logger(...)).
# 테스트 모듈보다 먼저 읽히는 여기서 임시 폴더를 지정해 실제 logs/ 에 테스트 기록이 섞이지 않게 한다.
os.environ["SKIN_LOG_DIR"] = tempfile.mkdtemp(prefix="skin-test-logs-")
