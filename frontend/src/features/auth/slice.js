import { createSlice, createAsyncThunk } from '@reduxjs/toolkit';
import cookie from "react-cookies";
import { API_BASE_PATH } from '@src/api/api.consts';

const saveCookie = (name, value) => {
    cookie.save(name, value, {
        path: '/',
        sameSite: 'lax',
        secure: false,
        maxAge: 86400
    });
};

export const postAuth = createAsyncThunk(
    'auth/postAuth',
    async ({ username, password }, { rejectWithValue }) => {
        try {
            const base64Credentials = btoa(`${username}:${password}`);
            
            const response = await fetch(`${API_BASE_PATH}/auth/check`, {
                method: 'GET',
                headers: {
                    'Authorization': `Basic ${base64Credentials}`,
                    'Accept': 'application/json'
                },
                credentials: 'include'
            });
            
            console.log('Auth response status:', response.status);
            
            if (response.ok) {
                saveCookie('username', username);
                saveCookie('password', password);
                
                return 'basic_auth_token';
            } else {
                const errorText = await response.text();
                console.error('Auth error:', response.status, errorText);
                return rejectWithValue('Неверное имя пользователя или пароль');
            }
        } catch (error) {
            console.error('Network or other error:', error);
            return rejectWithValue('Ошибка при подключении к серверу');
        }
    }
);

export const authSelector = (state) => state.auth;

const authSlice = createSlice({
    name: 'auth',
    initialState: {
        isAuth: false,
        token: null,
        username: "",
        password: "",
        show: false,
        loading: true,
        authError: null,
    },
    reducers: {
        setIsAuth: (state, action) => { state.isAuth = action.payload },
        setToken: (state, action) => { state.token = action.payload },
        setUserName: (state, action) => { 
            state.username = action.payload; 
        },
        setPassword: (state, action) => { 
            state.password = action.payload;
        },
        setShow: (state, action) => { state.show = action.payload },
        setLoading: (state, action) => { state.loading = action.payload },
        getIsAuth: (state) => { return state.isAuth },
        clearAuthError: (state) => {
            state.authError = null;
        }
    },
    extraReducers: (builder) => {
        builder.addCase(postAuth.pending, (state) => {
            state.loading = true;
            state.authError = null;
        });
        builder.addCase(postAuth.fulfilled, (state, action) => {
            console.log("PostAuth Success", action);
            state.token = action.payload;
            state.isAuth = true;
            state.show = false;
            state.loading = false;
            state.authError = null;
            
            if (state.remember) {
                localStorage.setItem("auth_username", state.username);
                localStorage.setItem("auth_password", state.password);
                localStorage.setItem("auth_remember", "true");
            }
        });
        builder.addCase(postAuth.rejected, (state, action) => {
            console.error("PostAuth Rejected", action.payload);
            state.isAuth = false;
            state.show = true;
            state.loading = false;   
            state.token = '';   
            state.authError = action.payload;       
        });
    },
});

export default authSlice.reducer;
export const {
    setIsAuth,
    setToken,
    setUserName,
    setPassword,
    setShow,
    setLoading,
    getIsAuth,
    clearAuthError
} = authSlice.actions;
