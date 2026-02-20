from setuptools import setup, find_packages

with open("requirements.txt") as f:
    install_requires = f.read().strip().split("\n")

setup(
    name="solara_wms",
    version="1.0.0",
    description="Warehouse Management System (WMS) for ERPNext",
    author="Win The Buy Box Private Limited",
    author_email="gopal@solara.in",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=install_requires,
)
