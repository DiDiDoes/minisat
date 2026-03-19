from pybind11.setup_helpers import Pybind11Extension, build_ext
from setuptools import setup

ext_modules = [
    Pybind11Extension(
        "minisat_wrapper",
        [
            "python/minisat_wrapper.cpp",
            "minisat/core/Solver.cc",
            "minisat/utils/Options.cc",
            "minisat/utils/System.cc",
        ],
        include_dirs=["."],
        define_macros=[
            ("__STDC_FORMAT_MACROS", "1"),
            ("__STDC_LIMIT_MACROS", "1"),
        ],
        libraries=["z"],
        cxx_std=14,
    )
]

setup(
    name="minisat-wrapper",
    version="0.1.0",
    description="Step-wise pybind11 wrapper for MiniSAT",
    ext_modules=ext_modules,
    cmdclass={"build_ext": build_ext},
    zip_safe=False,
)
