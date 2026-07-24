# Examples
Run an example with `jarlang examples/<name>.jlang`. Examples marked with (native) also work with the native compiler, e.g. `jarlang run --native examples/primes.jlang`.

* hello.jlang (native): variables, implicit multiplication, square roots, factorials, and functions
* fibonacci.jlang: recursion, loops, memoize, and large integers
* primes.jlang (native): the sieve of Eratosthenes, twin primes, and Goldbach's conjecture
* mandelbrot.jlang (native): the Mandelbrot set drawn with text
* numerics.jlang (native): Newton's method, the Leibniz series, and integration
* sorting.jlang (native): insertion sort and merge sort
* life.jlang (native): Conway's Game of Life
* benchmark.jlang (native): a program for `jarlang bench`
* functional.jlang: closures, functions as values, pipelines, and list comprehensions
* strings.jlang: strings, formatting, and dicts
* records.jlang: dicts used as records
* errors.jlang: try, catch, throw, and assert
* plotting.jlang: graphs drawn with `plot()`
* modules.jlang: importing another file (lib/geometry.jlang)

The output of each example is saved in `tests/golden`, and the tests check that it doesn't change.
