.PHONY: test lint fake run build check-dist smoke clean

test:                       ## 跑单元测试（不碰光驱/终端）
	python3 -m unittest discover -s tests -t . -v

RUFF ?= ruff

lint:                       ## 基础静态检查（需要先装 ruff：python3 -m pip install ruff）
	$(RUFF) check .

fake:                       ## 用合成光盘演示
	python3 asciicd.py --fake

run:                        ## 播真光驱里的 CD
	python3 asciicd.py

build:                      ## 打包 dist/（zipapp + 启动器 + 字体）
	python3 tools/build_dist.py

check-dist: build           ## 验收 dist/：文件、权限、字体、许可、归档、CLI 版本
	python3 tools/check_dist.py

smoke:                      ## 伪终端冒烟：画面、退出码、终端恢复
	python3 tools/pty_smoke.py --check

clean:                      ## 清掉构建产物和 __pycache__
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf dist
