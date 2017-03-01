<img src="http://i.imgur.com/CdzJiFi.png" align="right" width="150px"></img>
# PPYM

PPYM is the [Node.py] package manager.

  [Node.py]: https://github.com/nodepy/nodepy

__Synopsis__

    ppym init                    (initialize a package.json)
    ppym dist                    (create a .tar.gz archive from the current package)
    ppym register                (register a new account on the package registry)
    ppym upload <filename>       (upload a file to the package registry)
    ppym install [-g]            (install all dependencies of the current package)
    ppym install [-g] .          (install a package from a directory)
    ppym install [-g] <filename>
        (install a package from an archive)
    ppym install [-g] [@<scope>/]<package>
        (install a package from the PPYM package registry)
    ppym uninstall [-g] [@<scope>/]<package>
        (uninstall a previously installed package)
