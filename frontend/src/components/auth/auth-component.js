import React, { useEffect, useState } from "react";
import { useDispatch, useSelector } from 'react-redux';
import Avatar from '@mui/material/Avatar';
import Button from '@mui/material/Button';
import TextField from '@mui/material/TextField';
import Box from '@mui/material/Box';
import LockOutlinedIcon from '@mui/icons-material/LockOutlined';
import Typography from '@mui/material/Typography';
import Container from '@mui/material/Container';
import CircularProgress from '@mui/material/CircularProgress';
import Fade from '@mui/material/Fade';
import Alert from '@mui/material/Alert';
import { useSnackbar } from 'notistack';
import { 
    setPassword, 
    authSelector, 
    setShow, 
    setLoading, 
    postAuth,
    setUserName, 
    setToken
} from '../../features/auth/slice';
import cookie from 'react-cookies';

export const AuthComponent = () => {
    const dispatch = useDispatch();
    const { enqueueSnackbar } = useSnackbar();
    const {username, password, token, remember, show, loading, authError} = useSelector(authSelector);
    const [manualSubmit, setManualSubmit] = useState(false);

    useEffect(() => {
        dispatch(setShow(true));
        dispatch(setLoading(false));
        
        const savedRemember = localStorage.getItem("auth_remember") === "true";
        if (savedRemember) {
            dispatch(setUserName(localStorage.getItem("auth_username") || ""));
            dispatch(setPassword(localStorage.getItem("auth_password") || ""));
        } else {
            dispatch(setUserName(""));
            dispatch(setPassword(""));
        }
    }, []);

    useEffect(() => {
        if (token === null || token === '') {
            return;
        }
        
        if (token && token.length > 0 && manualSubmit) {
            enqueueSnackbar(`Успешный вход в систему`, { variant: 'success' });
        } else if (manualSubmit) {
            enqueueSnackbar(`Неудачная попытка входа. Проверьте введенные данные.`, { variant: 'error' });
            dispatch(setToken(null));
        }
    }, [token, manualSubmit]);

    const TcpAuth = async () => {
        if (username === "" || password === "") {
            enqueueSnackbar(`Сначала введите данные для авторизации.`,{ variant: 'error' });
            return;
        }

        setManualSubmit(true);
        dispatch(setLoading(true));
        
        try {
            const result = await dispatch(postAuth({username: username, password: password}));
            
            if (postAuth.fulfilled.match(result)) {
                enqueueSnackbar(`Успешный вход в систему`, { variant: 'success' });
            } else {
                enqueueSnackbar(result.payload || `Неудачная попытка входа`, { variant: 'error' });
            }
        } catch (e) {
            enqueueSnackbar(`Произошла ошибка при авторизации: ${e.message}`,{ variant: 'error' });
        } finally {
            dispatch(setLoading(false));
        }
    };



    const handleSubmit = (event) => {
        event.preventDefault();
        TcpAuth();
    };

    return (
        <Fade in={show}>
            <Box id="auth-container" sx={{ display: 'flex' }}>
                <Container component="main" maxWidth="xs">
                    <Box
                        sx={{
                            marginTop: 8,
                            display: 'flex',
                            flexDirection: 'column',
                            alignItems: 'center',
                        }}
                    >
                        <Avatar sx={{ m: 1, bgcolor: 'primary.main' }}>
                            <LockOutlinedIcon />
                        </Avatar>
                        <Typography component="h1" variant="h5" sx={{textAlign: "center"}}>
                            Войти в систему
                        </Typography>
                        {authError && (
                            <Alert severity="error" sx={{ mt: 2, width: '100%' }}>
                                {authError}
                            </Alert>
                        )}
                        <Box component="form" 
                        onSubmit={handleSubmit} 
                        noValidate sx={{ mt: 1, width: '100%' }}>
                            <TextField
                                margin="normal"
                                required
                                fullWidth
                                id="auth-username"
                                label="Логин"
                                name="username"
                                autoComplete="username"
                                autoFocus
                                disabled={loading}
                                value={username}
                                onChange={(e) => dispatch(setUserName(e.currentTarget.value))}
                            />
                            <TextField
                                margin="normal"
                                required
                                fullWidth
                                name="password"
                                label="Пароль"
                                type="password"
                                id="auth-password"
                                autoComplete="current-password"
                                disabled={loading}
                                value={password}
                                onChange={(e) => dispatch(setPassword(e.currentTarget.value))}
                            />
                            <Button
                                type="submit"
                                fullWidth
                                variant="contained"
                                disabled={loading}
                                sx={{ mt: 3, mb: 2 }}
                            >
                                Войти
                            </Button>
                            
                            <Typography variant="body2" color="text.secondary" sx={{ mt: 2, textAlign: 'center' }}>
                                Для получения доступа обратитесь к администратору системы
                            </Typography>
                            
                            {loading ?
                                <Box sx={{ display: 'flex', justifyContent: "center", mt: 2 }}>
                                    <CircularProgress />
                                </Box>
                                : ""
                            }
                        </Box>
                    </Box>
                </Container>
            </Box>
        </Fade>
    );
};
